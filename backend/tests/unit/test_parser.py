from app.webhook.whatsapp.parser import parse_normalized_messages
from app.webhook.whatsapp.schemas import WebhookEnvelope

PEPPER = "test-pepper"


def _envelope(messages=None, statuses=None) -> WebhookEnvelope:
    return WebhookEnvelope.model_validate(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba-id",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": "123"},
                                "messages": messages or [],
                                "statuses": statuses or [],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }
    )


def test_parses_text_message():
    envelope = _envelope(
        messages=[
            {
                "from": "15551234567",
                "id": "wamid.ABC123",
                "timestamp": "1699999999",
                "type": "text",
                "text": {"body": "Is this claim true?"},
            }
        ]
    )

    result = parse_normalized_messages(envelope, PEPPER)

    assert len(result) == 1
    msg = result[0]
    assert msg.wamid == "wamid.ABC123"
    assert msg.message_type == "text"
    assert msg.text_body == "Is this claim true?"
    assert msg.phone_hash  # populated, non-empty
    assert "15551234567" not in msg.phone_hash


def test_sanitizes_control_characters_and_caps_length():
    dirty_text = "hello\x00world" + ("x" * 5000)
    envelope = _envelope(
        messages=[
            {
                "from": "15551234567",
                "id": "wamid.CTRL",
                "type": "text",
                "text": {"body": dirty_text},
            }
        ]
    )

    result = parse_normalized_messages(envelope, PEPPER)

    assert "\x00" not in result[0].text_body
    assert len(result[0].text_body) <= 4096


def test_parses_image_metadata_only_no_download():
    envelope = _envelope(
        messages=[
            {
                "from": "15551234567",
                "id": "wamid.IMG",
                "type": "image",
                "image": {
                    "id": "media-id-123",
                    "mime_type": "image/jpeg",
                    "sha256": "abcd",
                    "caption": "look at this",
                },
            }
        ]
    )

    result = parse_normalized_messages(envelope, PEPPER)

    assert len(result) == 1
    msg = result[0]
    assert msg.message_type == "image"
    assert msg.media is not None
    assert msg.media.media_id == "media-id-123"
    assert msg.media.mime_type == "image/jpeg"


def test_unsupported_type_is_flagged_not_dropped():
    envelope = _envelope(
        messages=[
            {
                "from": "15551234567",
                "id": "wamid.LOC",
                "type": "location",
                "location": {"latitude": 1.0, "longitude": 2.0},
            }
        ]
    )

    result = parse_normalized_messages(envelope, PEPPER)

    assert len(result) == 1
    assert result[0].message_type == "unsupported"
    assert result[0].raw_type == "location"


def test_statuses_are_ignored_not_returned_as_messages():
    envelope = _envelope(
        messages=[],
        statuses=[{"id": "wamid.STATUS1", "status": "delivered", "recipient_id": "15551234567"}],
    )

    result = parse_normalized_messages(envelope, PEPPER)

    assert result == []


def test_message_missing_wamid_is_skipped():
    envelope = _envelope(
        messages=[{"from": "15551234567", "type": "text", "text": {"body": "no id"}}]
    )

    result = parse_normalized_messages(envelope, PEPPER)

    assert result == []


def test_message_with_invalid_from_is_skipped():
    envelope = _envelope(
        messages=[
            {"from": "not-a-phone-number", "id": "wamid.BAD", "type": "text", "text": {"body": "hi"}}
        ]
    )

    result = parse_normalized_messages(envelope, PEPPER)

    assert result == []


def test_empty_text_body_is_marked_unsupported():
    envelope = _envelope(
        messages=[{"from": "15551234567", "id": "wamid.EMPTY", "type": "text", "text": {"body": "   "}}]
    )

    result = parse_normalized_messages(envelope, PEPPER)

    assert len(result) == 1
    assert result[0].message_type == "unsupported"
