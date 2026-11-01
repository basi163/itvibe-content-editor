from __future__ import annotations

import asyncio
import functools
import io
import logging
import os
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from PIL import Image, ImageOps
from selenium import webdriver
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.file_detector import LocalFileDetector
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)


class VkBrowserError(RuntimeError):
    pass


class VkBrowserAuthRequired(VkBrowserError):
    pass


DriverFactory = Callable[[], WebDriver]


class VkBrowserPublisher:
    """Publish a VK wall post through the regular web composer.

    VK no longer lets a community token upload wall photos.  This publisher
    deliberately uses the same first-party composer an administrator uses in
    a browser.  The browser profile lives in the Selenium container, so login
    is performed once through its local-only noVNC window.
    """

    def __init__(
        self,
        webdriver_url: str,
        *,
        timeout: float = 45.0,
        client: httpx.AsyncClient | None = None,
        driver_factory: DriverFactory | None = None,
    ) -> None:
        self.webdriver_url = webdriver_url.strip()
        if not self.webdriver_url:
            raise ValueError("VK browser WebDriver URL is required")
        self.timeout = max(15.0, float(timeout))
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/131.0 Safari/537.36"
                )
            },
        )
        self._driver_factory = driver_factory or self._create_driver
        self._driver: WebDriver | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="vk-browser",
        )

    async def close(self) -> None:
        try:
            await self._run(self._close_driver)
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)
            if self._owns_client:
                await self.client.aclose()

    async def open_group(self, group_id: int) -> None:
        """Open the group page and keep the browser alive for interactive login."""
        await self._run(self._open_group, int(group_id))

    async def publish_article(
        self,
        *,
        group_id: int,
        message: str,
        article_url: str,
    ) -> None:
        image_path = await self._download_article_preview(article_url)
        try:
            await self._run(
                self._publish,
                int(group_id),
                message.strip(),
                image_path,
            )
        finally:
            try:
                image_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temporary VK cover")

    async def _run(self, function: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor,
            functools.partial(function, *args),
        )

    async def _download_article_preview(self, article_url: str) -> Path:
        response = await self.client.get(article_url)
        response.raise_for_status()
        document = BeautifulSoup(response.text, "html.parser")
        image_url = ""
        for selector in (
            "meta[property='og:image']",
            "meta[name='twitter:image']",
            "meta[property='vk:image']",
        ):
            element = document.select_one(selector)
            if element and element.get("content"):
                image_url = urljoin(article_url, str(element["content"]).strip())
                break
        if not image_url.startswith(("http://", "https://")):
            raise VkBrowserError("Teletype article has no public preview image")

        image_response = await self.client.get(
            image_url,
            headers={"Referer": article_url},
        )
        image_response.raise_for_status()
        temporary_path: Path | None = None
        try:
            with Image.open(io.BytesIO(image_response.content)) as source:
                prepared = ImageOps.exif_transpose(source).convert("RGB")
                prepared.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
                with tempfile.NamedTemporaryFile(
                    prefix="itvibe-vk-cover-",
                    suffix=".jpg",
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    prepared.save(
                        temporary,
                        format="JPEG",
                        quality=92,
                        optimize=True,
                    )
        except (OSError, ValueError) as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise VkBrowserError("VK cover image cannot be prepared") from error
        if temporary_path is None:
            raise VkBrowserError("VK cover image cannot be prepared")
        return temporary_path

    def _create_driver(self) -> WebDriver:
        options = webdriver.ChromeOptions()
        options.add_argument("--user-data-dir=/home/seluser/vk-profile")
        options.add_argument("--profile-directory=Default")
        options.add_argument("--lang=ru-RU")
        options.add_argument("--window-size=1440,1200")
        options.add_argument("--disable-notifications")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        driver = webdriver.Remote(
            command_executor=self.webdriver_url,
            options=options,
        )
        driver.file_detector = LocalFileDetector()
        driver.set_page_load_timeout(self.timeout)
        driver.set_window_size(1440, 1200)
        return driver

    def _ensure_driver(self) -> WebDriver:
        if self._driver is not None:
            try:
                _ = self._driver.current_url
                return self._driver
            except (InvalidSessionIdException, NoSuchWindowException, WebDriverException):
                self._close_driver()
        try:
            self._driver = self._driver_factory()
        except WebDriverException as error:
            raise VkBrowserError("VK browser service is unavailable") from error
        return self._driver

    def _close_driver(self) -> None:
        driver, self._driver = self._driver, None
        if driver is None:
            return
        try:
            driver.quit()
        except WebDriverException:
            pass

    def _open_group(self, group_id: int) -> None:
        driver = self._ensure_driver()
        try:
            driver.get(f"https://vk.ru/club{group_id}")
        except TimeoutException:
            logger.info("VK group page is still loading in the browser")
        if self._is_logged_out(driver):
            raise VkBrowserAuthRequired(
                "Нужно один раз войти в VK через защищённое окно браузера"
            )

    def _publish(self, group_id: int, message: str, image_path: Path) -> None:
        if not message:
            raise VkBrowserError("VK post text is empty")
        self._open_group(group_id)
        driver = self._ensure_driver()

        trigger = self._wait_for_first(
            driver,
            (
                (By.CSS_SELECTOR, "[data-testid='wall-post-input']"),
                (By.CSS_SELECTOR, "[data-testid='wall_post_input']"),
                (By.CSS_SELECTOR, "[aria-label*='Создать запись']"),
                (
                    By.XPATH,
                    (
                        "//*[self::button or @role='button' or @contenteditable='true']"
                        "[contains(., 'Создать запись') or "
                        "contains(., 'Что у вас нового') or "
                        "contains(., 'Добавить запись') or "
                        "contains(., 'Написать пост')]"
                    ),
                ),
            ),
            timeout=self.timeout,
        )
        if trigger is None:
            if self._is_logged_out(driver):
                raise VkBrowserAuthRequired(
                    "Сессия VK завершена: нужен повторный вход"
                )
            raise VkBrowserError(
                "Не удалось найти редактор поста VK; проверьте права администратора"
            )
        self._safe_click(driver, trigger)

        editor = self._wait_for_editor(driver)
        if editor is None:
            raise VkBrowserError("Окно создания поста VK не открылось")
        self._safe_click(driver, editor)
        editor.send_keys(Keys.CONTROL, "a")
        editor.send_keys(Keys.BACKSPACE)
        editor.send_keys(message)

        file_input = self._find_photo_input(driver)
        if file_input is None:
            photo_button = self._wait_for_first(
                driver,
                (
                    (By.CSS_SELECTOR, "[aria-label*='Фото']"),
                    (By.CSS_SELECTOR, "[title*='Фото']"),
                    (
                        By.XPATH,
                        (
                            "//*[self::button or @role='button']"
                            "[contains(@aria-label, 'Фото') or contains(., 'Фото')]"
                        ),
                    ),
                ),
                timeout=8,
            )
            if photo_button is not None:
                self._safe_click(driver, photo_button)
                file_input = self._wait_for_photo_input(driver, timeout=10)
        if file_input is None:
            raise VkBrowserError("В редакторе VK не найдена загрузка фото")

        try:
            driver.execute_script(
                "arguments[0].style.display='block';"
                "arguments[0].style.visibility='visible';",
                file_input,
            )
        except WebDriverException:
            pass
        file_input.send_keys(os.fspath(image_path))
        self._wait_for_upload(driver, file_input)

        submit = self._wait_for_first(
            driver,
            (
                (
                    By.XPATH,
                    (
                        "//*[self::button or @role='button']"
                        "[normalize-space(.)='Опубликовать' or "
                        "normalize-space(.)='Разместить' or "
                        "normalize-space(.)='Отправить']"
                    ),
                ),
                (By.CSS_SELECTOR, "button[type='submit']"),
            ),
            timeout=self.timeout,
            require_enabled=True,
        )
        if submit is None:
            raise VkBrowserError("Кнопка публикации VK недоступна")
        self._safe_click(driver, submit)
        try:
            WebDriverWait(driver, self.timeout).until(
                lambda current: not editor.is_displayed()
            )
        except (TimeoutException, WebDriverException):
            # VK can leave the composer mounted after a successful async submit.
            # The API verification performed by VkPublisher is authoritative.
            logger.info("VK composer did not close before verification")

    def _wait_for_editor(self, driver: WebDriver) -> Any | None:
        def locate(current: WebDriver) -> Any | None:
            candidates = current.find_elements(
                By.CSS_SELECTOR,
                "[contenteditable='true'], textarea",
            )
            usable = []
            for element in candidates:
                try:
                    size = element.size
                    if element.is_displayed() and size["width"] >= 280:
                        usable.append(element)
                except WebDriverException:
                    continue
            return usable[-1] if usable else None

        try:
            return WebDriverWait(driver, self.timeout).until(locate)
        except TimeoutException:
            return None

    def _find_photo_input(self, driver: WebDriver) -> Any | None:
        for element in reversed(driver.find_elements(By.CSS_SELECTOR, "input[type='file']")):
            try:
                accepted = (element.get_attribute("accept") or "").casefold()
            except WebDriverException:
                continue
            if not accepted or "image" in accepted or ".jpg" in accepted:
                return element
        return None

    def _wait_for_photo_input(self, driver: WebDriver, *, timeout: float) -> Any | None:
        try:
            return WebDriverWait(driver, timeout).until(
                lambda current: self._find_photo_input(current)
            )
        except TimeoutException:
            return None

    def _wait_for_upload(self, driver: WebDriver, file_input: Any) -> None:
        try:
            WebDriverWait(driver, self.timeout).until(
                lambda current: bool(file_input.get_attribute("value"))
                or any(
                    element.is_displayed()
                    for element in current.find_elements(
                        By.CSS_SELECTOR,
                        "img[src^='blob:'], [class*='Photo'] img, [class*='Media'] img",
                    )
                )
            )
        except TimeoutException as error:
            raise VkBrowserError("Обложка не загрузилась в VK") from error

    @staticmethod
    def _safe_click(driver: WebDriver, element: Any) -> None:
        try:
            element.click()
        except WebDriverException:
            driver.execute_script("arguments[0].click();", element)

    @staticmethod
    def _is_logged_out(driver: WebDriver) -> bool:
        try:
            url = driver.current_url.casefold()
            if "login.vk." in url or "id.vk." in url or "/login" in url:
                return True
            return bool(
                driver.find_elements(
                    By.CSS_SELECTOR,
                    "input[name='login'], input[name='phone'], [data-testid='login_button']",
                )
            )
        except WebDriverException:
            return False

    @staticmethod
    def _wait_for_first(
        driver: WebDriver,
        locators: tuple[tuple[str, str], ...],
        *,
        timeout: float,
        require_enabled: bool = False,
    ) -> Any | None:
        def locate(current: WebDriver) -> Any | None:
            for by, value in locators:
                for element in current.find_elements(by, value):
                    try:
                        if not element.is_displayed():
                            continue
                        if require_enabled and not element.is_enabled():
                            continue
                        return element
                    except WebDriverException:
                        continue
            return None

        try:
            return WebDriverWait(driver, timeout).until(locate)
        except TimeoutException:
            return None
