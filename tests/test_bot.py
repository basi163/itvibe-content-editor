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
