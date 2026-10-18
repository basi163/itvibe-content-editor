from aiogram.types import InputMediaPhoto, InputMediaVideo

from app.bot import EditorBot


def test_rich_photo_is_embedded_in_one_message() -> None:
    message = EditorBot._rich_message("<h2>Title</h2><p>Body</p>", "photo-file-id", "photo")

    assert message.html == '<img src="tg://photo?id=cover"/><h2>Title</h2><p>Body</p>'
    assert message.media is not None
    assert len(message.media) == 1
    assert isinstance(message.media[0].media, InputMediaPhoto)


def test_rich_video_is_embedded_in_one_message() -> None:
    message = EditorBot._rich_message("<p>Body</p>", "video-file-id", "video")

    assert message.html == '<video src="tg://video?id=cover"></video><p>Body</p>'
    assert message.media is not None
    assert isinstance(message.media[0].media, InputMediaVideo)


def test_draft_keyboard_describes_automatic_edits() -> None:
    keyboard = EditorBot._draft_keyboard(7)
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    labels = {button.text for button in buttons}
    callbacks = {button.callback_data for button in buttons}

    assert "Другой заголовок" in labels
    assert "Другие хэштеги" in labels
    assert "Другое изображение" in labels
    assert "image:7" in callbacks
    assert "media:7" not in callbacks
