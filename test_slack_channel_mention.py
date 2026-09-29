import slack_notifier


def test_with_channel_mention_prefixes_text_and_visible_mrkdwn_block():
    text, blocks = slack_notifier.with_channel_mention(
        '運動を実施しましたか？',
        [
            {'type': 'section', 'text': {'type': 'mrkdwn', 'text': '運動を実施しましたか？'}},
            {'type': 'actions', 'elements': []},
        ],
    )

    assert text == '<!channel>\n運動を実施しましたか？'
    assert blocks[0]['text']['text'] == '<!channel>\n運動を実施しましたか？'


def test_with_channel_mention_does_not_add_duplicate_prefix():
    text, blocks = slack_notifier.with_channel_mention(
        '<!channel>\n通知があります。',
        [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': '<!channel>\n通知があります。'}}],
    )

    assert text == '<!channel>\n通知があります。'
    assert blocks[0]['text']['text'] == '<!channel>\n通知があります。'