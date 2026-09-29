"""Slack exercise survey flow.

Flow:
1. main.py detects motion/no-motion.
2. This module sends a pre-survey only for no-motion.
3. The pre-survey answer is carried into the AI prompt and stored in DB.
4. The exercise message is sent.
5. Post-survey is shown and stored in DB.

This file can be run standalone, and it also exposes helpers that main.py can call.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Any, Dict, Optional

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

try:
    import tokens_db
    tokens_db.ensure_schema()
except Exception as exc:
    print(f"Warning: tokens_db import failed: {exc}")
    tokens_db = None

try:
    import storage_sqlite
    storage_sqlite.ensure_schema()
except Exception as exc:
    print(f"Warning: storage_sqlite import failed: {exc}")
    storage_sqlite = None

try:
    import use_openai
except Exception:
    use_openai = None

import slack_notifier


EXERCISE_LABELS = {
    'stretch': 'ストレッチ',
    'cardio': '有酸素',
    'strength': '筋トレ',
}

REASON_LABELS = {
    'tired': '疲れている',
    'no_time': '時間がない',
    'no_motivation': '気分が乗らない',
    'other': 'その他',
}


def _sanitize_user_id(user_id: str) -> str:
    import re
    return re.sub(r'[^0-9A-Za-z]+', '_', user_id or 'unknown')


def save_survey_result(
    user_id: str,
    exercise_type: str,
    implemented: str,
    pre_survey: Optional[dict] = None,
    post_survey: Optional[dict] = None,
):
    safe_user = _sanitize_user_id(user_id)
    msg_dir = os.path.join('data_output', safe_user, 'message')
    os.makedirs(msg_dir, exist_ok=True)

    now = datetime.now()
    filepath = os.path.join(msg_dir, f'message_{now.strftime("%Y-%m-%d")}.json')
    payload = {
        'user_id': user_id,
        'timestamp': now.isoformat(),
        'exercise_type': exercise_type,
        'implemented': implemented,
        'pre_survey': pre_survey or {},
        'post_survey': post_survey or {},
    }

    data = {'messages': []}
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
        except Exception:
            data = {'messages': []}
    if not isinstance(data.get('messages'), list):
        data['messages'] = []
    data['messages'].append(payload)
    with open(filepath, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    return filepath


def resolve_channel(cli_arg: Optional[str], user_id: Optional[str] = None) -> Optional[str]:
    """Resolve channel by CLI arg, env, or tokens_db lookup for given user_id.

    If user_id is provided, prefer tokens_db lookup for that user.
    """
    if cli_arg:
        return cli_arg
    env_ch = os.environ.get('SLACK_CHANNEL')
    if env_ch:
        return env_ch
    if tokens_db and user_id:
        try:
            return tokens_db.get_channel_for_user(user_id)
        except Exception:
            return None
    return None


def build_pre_survey_blocks() -> list:
    return [
        {
            'type': 'section',
            'text': {'type': 'mrkdwn', 'text': '人目に付きますか？'},
            'accessory': {
                'type': 'radio_buttons',
                'action_id': 'pre_survey_visibility',
                'options': [
                    {'text': {'type': 'plain_text', 'text': 'はい'}, 'value': 'yes'},
                    {'text': {'type': 'plain_text', 'text': 'いいえ'}, 'value': 'no'},
                ],
            },
        },
        {
            'type': 'actions',
            'elements': [
                {
                    'type': 'button',
                    'text': {'type': 'plain_text', 'text': '次へ'},
                    'style': 'primary',
                    'action_id': 'submit_pre_survey',
                    'value': 'submit_pre_survey',
                }
            ],
        },
    ]


def build_post_implemented_blocks() -> list:
    return [
        {
            'type': 'section',
            'text': {'type': 'mrkdwn', 'text': '運動を実施しましたか？'},
            'accessory': {
                'type': 'radio_buttons',
                'action_id': 'post_survey_implemented',
                'options': [
                    {'text': {'type': 'plain_text', 'text': '実施した'}, 'value': 'yes'},
                    {'text': {'type': 'plain_text', 'text': '実施しなかった'}, 'value': 'no'},
                ],
            },
        },
        {
            'type': 'actions',
            'elements': [
                {
                    'type': 'button',
                    'text': {'type': 'plain_text', 'text': '次へ'},
                    'style': 'primary',
                    'action_id': 'submit_post_implemented',
                    'value': 'submit_post_implemented',
                }
            ],
        },
    ]


def build_post_reason_blocks() -> list:
    return [
        {
            'type': 'section',
            'text': {'type': 'mrkdwn', 'text': '未実施の理由を教えてください'},
            'accessory': {
                'type': 'static_select',
                'action_id': 'post_survey_reason',
                'placeholder': {'type': 'plain_text', 'text': '理由を選択'},
                'options': [
                    {'text': {'type': 'plain_text', 'text': '場所が適切でない'}, 'value': 'tired'},
                    {'text': {'type': 'plain_text', 'text': '時間がない'}, 'value': 'no_time'},
                    {'text': {'type': 'plain_text', 'text': '気分が乗らない'}, 'value': 'no_motivation'},
                    {'text': {'type': 'plain_text', 'text': 'その他'}, 'value': 'other'},
                ],
            },
        },
        {
            'type': 'actions',
            'elements': [
                {
                    'type': 'button',
                    'text': {'type': 'plain_text', 'text': '次へ'},
                    'style': 'primary',
                    'action_id': 'submit_post_reason',
                    'value': 'submit_post_reason',
                }
            ],
        },
    ]


def build_post_intensity_blocks() -> list:
    return [
        {
            'type': 'section',
            'text': {'type': 'mrkdwn', 'text': '運動の強度を教えてください。\n値が高いほど、強度が高かったとします。（1～10）'},
            'accessory': {
                'type': 'static_select',
                'action_id': 'post_survey_intensity',
                'placeholder': {'type': 'plain_text', 'text': '選択してください'},
                'options': [
                    {'text': {'type': 'plain_text', 'text': str(i)}, 'value': str(i)}
                    for i in range(1, 11)
                ],
            },
        },
        {
            'type': 'actions',
            'elements': [
                {
                    'type': 'button',
                    'text': {'type': 'plain_text', 'text': '次へ'},
                    'style': 'primary',
                    'action_id': 'submit_post_intensity',
                    'value': 'submit_post_intensity',
                }
            ],
        },
    ]


def build_post_survey_blocks(exercise_type: str) -> list:
    # Backward-compatible name: first ask whether the exercise was implemented.
    _ = exercise_type
    return build_post_implemented_blocks()


def build_other_reason_modal(notification_id: Optional[str] = None, message_ts: Optional[str] = None) -> dict:
    modal = {
        'type': 'modal',
        'callback_id': 'other_reason_modal',
        'title': {'type': 'plain_text', 'text': 'その他の理由'},
        'submit': {'type': 'plain_text', 'text': '保存'},
        'close': {'type': 'plain_text', 'text': 'キャンセル'},
        'blocks': [
            {
                'type': 'input',
                'block_id': 'other_reason_block',
                'label': {'type': 'plain_text', 'text': 'その他の理由を入力してください'},
                'element': {
                    'type': 'plain_text_input',
                    'action_id': 'other_reason_input',
                    'multiline': True,
                },
            }
        ],
    }
    if notification_id or message_ts:
        modal['private_metadata'] = json.dumps({
            'notification_id': notification_id,
            'message_ts': message_ts,
        })
    return modal


def _fallback_exercise_message(exercise_type: str, detection_type: Optional[str] = None) -> str:
    _ = exercise_type
    event_type = 'activity' if detection_type == 'motion' else 'no_activity'
    try:
        import comment_templates
        return comment_templates.get_comment(event_type)
    except Exception:
        logging.exception(
            'exercise template fallback failed event_type=%s detection_type=%s',
            event_type,
            detection_type,
        )
        return ''


def _generate_exercise_payload(
    user_id: str,
    exercise_type: str,
    pre_survey: dict,
    ai_context: Optional[dict],
    detection_type: Optional[str] = None,
) -> dict:
    if detection_type is None and isinstance(ai_context, dict):
        detection_type = ai_context.get('detection_type')
    fallback_message = _fallback_exercise_message(exercise_type, detection_type)
    if use_openai is None:
        logging.warning(
            'exercise payload fallback: use_openai unavailable user=%s exercise_type=%s',
            user_id,
            exercise_type,
        )
        return {'category': exercise_type, 'message': fallback_message, 'rationale': 'fallback', 'reps': None, 'sets': None, 'duration_min': None}

    context = {
        'pre_survey': pre_survey,
        'source': 'send_slack_checkbox',
    }
    if ai_context:
        context.update(ai_context)
    detection_type = context.get('detection_type')
    # Do not hard-fix exercise_type for either detection type.
    # The AI will choose from recommended_types listed in context.
    context.pop('exercise_type', None)
    if pre_survey.get('visibility') is not None:
        context['visibility_before'] = pre_survey['visibility']

    try:
        event_type = 'exercise_suggestion'
        payload = use_openai.generate_notification_payload(
            event_type=event_type,
            user_id=user_id,
            context=context,
            fallback_message=fallback_message,
        )
        if not isinstance(payload, dict):
            logging.warning(
                'exercise payload fallback: non-dict payload user=%s exercise_type=%s detection_type=%s payload_type=%s',
                user_id,
                exercise_type,
                detection_type,
                type(payload).__name__,
            )
            return {'category': exercise_type, 'message': fallback_message, 'rationale': 'fallback', 'reps': None, 'sets': None, 'duration_min': None}
        if not payload.get('message'):
            logging.warning(
                'exercise payload missing message; using fallback text user=%s exercise_type=%s detection_type=%s',
                user_id,
                exercise_type,
                detection_type,
            )
            payload['message'] = fallback_message
        if not payload.get('category'):
            logging.warning(
                'exercise payload missing category; using fallback category user=%s exercise_type=%s detection_type=%s',
                user_id,
                exercise_type,
                detection_type,
            )
            payload['category'] = exercise_type
        return payload
    except Exception:
        logging.exception(
            'exercise payload fallback due to exception user=%s exercise_type=%s detection_type=%s',
            user_id,
            exercise_type,
            detection_type,
        )
        return {'category': exercise_type, 'message': fallback_message, 'rationale': 'fallback', 'reps': None, 'sets': None, 'duration_min': None}


def _exercise_name(exercise_type: str) -> str:
    return EXERCISE_LABELS.get(exercise_type, exercise_type)


def _extract_selected_value(body: dict, action_id: str) -> Optional[str]:
    """Extract selected value for a Block Kit action from either state.values or actions payload."""
    state_values = body.get('state', {}).get('values', {})
    for actions in state_values.values():
        for current_action_id, action_data in actions.items():
            if current_action_id == action_id:
                selected_opt = action_data.get('selected_option', {})
                value = selected_opt.get('value')
                if value is not None:
                    return value

    for action in body.get('actions', []):
        if action.get('action_id') == action_id:
            selected_opt = action.get('selected_option') or {}
            value = selected_opt.get('value')
            if value is not None:
                return value
    return None


@dataclass
class SurveySession:
    user_id: str
    channel: str
    exercise_type: str
    motion_detected: bool
    ai_context: dict = field(default_factory=dict)
    ai_payload: dict = field(default_factory=dict)
    message_ts: Optional[str] = None
    notification_id: Optional[str] = None
    detection_type: Optional[str] = None
    pre_survey: dict = field(default_factory=dict)
    post_survey: dict = field(default_factory=dict)
    post_stage: str = 'implemented'
    displayed_followup_keys: set[str] = field(default_factory=set)
    followup_lock: Any = field(default_factory=Lock, repr=False)


class _SessionProxy:
    """Forward legacy session attribute access to the current Slack action session."""

    def __init__(self, current: ContextVar[SurveySession]):
        object.__setattr__(self, '_current', current)

    def __getattr__(self, name: str):
        return getattr(self._current.get(), name)

    def __setattr__(self, name: str, value: Any):
        setattr(self._current.get(), name, value)


def create_app(bot_token: str, app_token: str, session: SurveySession) -> App:
    app = App(token=bot_token)
    default_session = session
    current_session = ContextVar('current_survey_session', default=default_session)
    sessions_by_message_ts: Dict[str, SurveySession] = {}
    sessions_lock = Lock()
    session = _SessionProxy(current_session)

    def _get_or_restore_session(body: dict) -> Optional[SurveySession]:
        if not isinstance(body, dict):
            return None
        message_ts = body.get('message', {}).get('ts') or body.get('container', {}).get('message_ts')
        if not message_ts:
            return None

        with sessions_lock:
            restored = sessions_by_message_ts.get(message_ts)
            if restored is None and storage_sqlite:
                try:
                    record = storage_sqlite.get_survey_session_by_message_ts(message_ts)
                except Exception:
                    logging.exception('Failed to restore survey session for message_ts=%s', message_ts)
                    record = None
                if record and record.get('user_id'):
                    restored = SurveySession(
                        user_id=record['user_id'],
                        channel=record.get('channel') or '',
                        exercise_type=record.get('exercise_type') or 'stretch',
                        motion_detected=record.get('detection_type') == 'motion',
                        ai_context=record.get('ai_context') or {},
                        ai_payload={'message': record.get('message_text') or ''},
                        message_ts=message_ts,
                        notification_id=record.get('notification_id'),
                        detection_type=record.get('detection_type'),
                    )
                    sessions_by_message_ts[message_ts] = restored
            if restored is None:
                logging.warning('Survey session not found for message_ts=%s', message_ts)
                return None

        current_session.set(restored)
        return restored

    def _get_or_restore_session_from_view(view: dict) -> Optional[SurveySession]:
        metadata = view.get('private_metadata') if isinstance(view, dict) else None
        try:
            identifiers = json.loads(metadata) if metadata else {}
        except (TypeError, ValueError):
            identifiers = {}
        message_ts = identifiers.get('message_ts')
        notification_id = identifiers.get('notification_id')

        with sessions_lock:
            restored = sessions_by_message_ts.get(message_ts) if message_ts else None
            if restored is None and storage_sqlite and notification_id:
                try:
                    record = storage_sqlite.get_survey_session_by_notification_id(notification_id)
                except Exception:
                    logging.exception('Failed to restore survey session for notification_id=%s', notification_id)
                    record = None
                if record and record.get('user_id'):
                    restored = SurveySession(
                        user_id=record['user_id'],
                        channel=record.get('channel') or '',
                        exercise_type=record.get('exercise_type') or 'stretch',
                        motion_detected=record.get('detection_type') == 'motion',
                        ai_context=record.get('ai_context') or {},
                        ai_payload={'message': record.get('message_text') or ''},
                        message_ts=message_ts,
                        notification_id=record.get('notification_id'),
                        detection_type=record.get('detection_type'),
                    )
                    if message_ts:
                        sessions_by_message_ts[message_ts] = restored
            if restored is None:
                logging.warning('Survey session not found for modal notification_id=%s', notification_id)
                return None

        current_session.set(restored)
        return restored

    def _claim_followup_display(followup_type: str) -> bool:
        notification_key = session.notification_id or session.message_ts
        if not notification_key:
            return True
        display_key = f'{notification_key}:{followup_type}'
        with session.followup_lock:
            if display_key in session.displayed_followup_keys:
                logging.info(
                    'skip duplicate %s followup user=%s notification_key=%s',
                    followup_type,
                    session.user_id,
                    notification_key,
                )
                return False
            session.displayed_followup_keys.add(display_key)
        return True

    def _update_message(client, blocks: list, text: str):
        # Wrap Slack updates with error handling: if update fails (e.g. bot not in channel)
        # record failure and avoid raising to the Bolt handler thread.
        try:
            text, blocks = slack_notifier.with_channel_mention(text, blocks)
            # debug: log channel and ts used for the update/post
            try:
                import logging as _logging
                _logging.info('Attempting Slack update/post for user=%s channel=%s message_ts=%s', session.user_id, repr(session.channel), session.message_ts)
            except Exception:
                pass

            # Do not enqueue here. Enqueue + immediate post can race with poller and
            # produce duplicate messages for the same content.
            nid_row = None

            if session.message_ts:
                try:
                    client.chat_update(
                        channel=session.channel,
                        ts=session.message_ts,
                        text=text,
                        blocks=blocks,
                    )
                except Exception as _upd_e:
                    # If the original message no longer exists, fall back to posting a new one.
                    _upd_code = None
                    try:
                        _upd_code = _upd_e.response.get('error') if hasattr(_upd_e, 'response') else None
                    except Exception:
                        pass
                    if _upd_code == 'message_not_found':
                        try:
                            import logging as _logging
                            _logging.warning('chat.update message_not_found (ts=%s); falling back to postMessage', session.message_ts)
                        except Exception:
                            pass
                        session.message_ts = None
                        res = client.chat_postMessage(channel=session.channel, text=text, blocks=blocks)
                        try:
                            ts = res.get('ts') if isinstance(res, dict) else None
                            if ts:
                                session.message_ts = ts
                            if nid_row is not None:
                                storage_sqlite.mark_notification_sent(nid_row, message_ts=ts, notification_id=session.notification_id)
                        except Exception:
                            pass
                    else:
                        raise
            else:
                res = client.chat_postMessage(channel=session.channel, text=text, blocks=blocks)
                try:
                    ts = None
                    try:
                        ts = res.get('ts') if isinstance(res, dict) else None
                    except Exception:
                        ts = None
                    if ts:
                        session.message_ts = ts
                    if nid_row is not None:
                        try:
                            storage_sqlite.mark_notification_sent(nid_row, message_ts=ts, notification_id=session.notification_id)
                        except Exception:
                            try:
                                storage_sqlite.mark_notification_sent(nid_row, message_ts=ts)
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception as _e:
            # Avoid importing slack_sdk at module import time; handle generically.
            try:
                from slack_sdk.errors import SlackApiError
            except Exception:
                SlackApiError = None

            err_code = None
            try:
                if SlackApiError and isinstance(_e, SlackApiError):
                    # response may contain JSON with 'error' key
                    resp = getattr(_e, 'response', None)
                    if resp and isinstance(resp, dict):
                        err_code = resp.get('error')
                    else:
                        try:
                            err_code = _e.response['error'] if _e.response and 'error' in _e.response else None
                        except Exception:
                            err_code = None
                else:
                    # non-Slack error
                    err_code = None
            except Exception:
                err_code = None

            try:
                import logging
                logging.error('Slack send failed for user=%s channel=%s message_ts=%s err=%s code=%s', session.user_id, repr(session.channel), session.message_ts, str(_e), err_code)
            except Exception:
                pass

            try:
                if session.notification_id and storage_sqlite:
                    storage_sqlite.mark_notification_failed(session.notification_id, reason=err_code or str(_e))
            except Exception:
                pass

            try:
                resp = getattr(_e, 'response', None)
                if resp is not None:
                    try:
                        import logging as _logging
                        _logging.debug('Slack exception response: %s', resp)
                    except Exception:
                        pass
            except Exception:
                pass
            return

    def _sync_session_from_body(body: dict):
        """Populate `session` fields from an incoming Bolt payload (action/view/body).
        Uses message.ts to look up the corresponding notifications row and restore
        `session.user_id`, `session.channel`, and `session.notification_id` when possible.
        """
        try:
            if not body or not isinstance(body, dict):
                return
            # try common locations for message ts and channel id
            msg_ts = None
            try:
                msg_ts = body.get('message', {}).get('ts') or body.get('container', {}).get('message_ts')
            except Exception:
                msg_ts = None

            # Always sync message_ts from payload to avoid reusing stale ts
            # from a previous user's interaction in a long-lived Socket Mode process.
            try:
                session.message_ts = msg_ts
            except Exception:
                pass

            ch = None
            try:
                ch = (body.get('channel') or {}).get('id') or (body.get('container') or {}).get('channel_id')
            except Exception:
                ch = None

            # If channel found in payload, prefer it
            if ch:
                try:
                    session.channel = ch
                except Exception:
                    pass

            # If we have a message_ts, lookup notifications row to recover user/channel
            if msg_ts and storage_sqlite:
                try:
                    conn = storage_sqlite._get_conn()
                    try:
                        cur = conn.cursor()
                        # Prefer to retrieve the stored notification_id (text) that
                        # references sent_exercise_notifications.notification_id.
                        cur.execute('SELECT id, notification_id, user_id, channel FROM notifications WHERE message_ts = %s LIMIT 1', (msg_ts,))
                        row = cur.fetchone()
                        if row:
                            notif_row_id = row[0]
                            notif_notification_id = row[1]
                            uid = row[2]
                            db_ch = row[3]
                            try:
                                if uid:
                                    session.user_id = uid
                            except Exception:
                                pass
                            try:
                                if db_ch and not session.channel:
                                    session.channel = db_ch
                            except Exception:
                                pass
                            # If a textual notification_id (from sent_exercise_notifications)
                            # is available, prefer it so downstream update_response uses
                            # the correct identifier type. Otherwise fall back to row id.
                            try:
                                if notif_notification_id:
                                    session.notification_id = notif_notification_id
                                else:
                                    session.notification_id = str(notif_row_id)
                            except Exception:
                                pass
                            # Restore exercise metadata so motion/cardio/strength
                            # flows can branch correctly even in shared Socket Mode session.
                            try:
                                if storage_sqlite and session.notification_id:
                                    sent = storage_sqlite.get_sent_notification(session.notification_id)
                                    if isinstance(sent, dict):
                                        ex_type = sent.get('exercise_type')
                                        if ex_type in ('stretch', 'cardio', 'strength'):
                                            session.exercise_type = ex_type
                                        msg_text = sent.get('message_text')
                                        if isinstance(msg_text, str) and msg_text.strip():
                                            if not isinstance(session.ai_payload, dict):
                                                session.ai_payload = {}
                                            session.ai_payload['message'] = msg_text
                            except Exception:
                                pass
                    finally:
                        try:
                            conn.close()
                        except Exception:
                            pass
                except Exception:
                    # non-fatal; best-effort only
                    pass
        except Exception:
            pass

    def _start_exercise_flow(client, user_visibility: Optional[str] = None):
        if user_visibility is not None:
            session.pre_survey['visibility'] = user_visibility
            session.ai_payload = _generate_exercise_payload(
            session.user_id,
            session.exercise_type,
            session.pre_survey,
            session.ai_context,
                session.detection_type,
        )
            # AI may choose a different type than the placeholder used at launch
            # (e.g. no_movement's fallback 'stretch'); sync it so post-survey
            # branching (intensity question) and DB persistence use the real type.
            chosen_category = session.ai_payload.get('category')
            if chosen_category in ('stretch', 'cardio', 'strength'):
                session.exercise_type = chosen_category
            message_text = session.ai_payload.get('message') or _fallback_exercise_message(
                session.exercise_type,
                session.detection_type,
            )
        if storage_sqlite:
            # Keep the original notification_id for the same questionnaire session.
            # Do not create a new sent/pending pair after pre-survey answer.
            if not session.notification_id:
                session.notification_id = storage_sqlite.create_sent_notification(
                    user_id=session.user_id,
                    exercise_name=_exercise_name(session.exercise_type),
                    exercise_type=session.exercise_type,
                    message_text=message_text,
                    ai_category=session.ai_payload.get('category'),
                    ai_rationale=session.ai_payload.get('rationale'),
                    reps=session.ai_payload.get('reps'),
                    sets=session.ai_payload.get('sets'),
                    duration_min=session.ai_payload.get('duration_min'),
                    ai_context=session.ai_context,
                )
                storage_sqlite.create_pending_response(session.notification_id, session.user_id)
            else:
                # The row was already created by launch_session before pre_survey
                # was answered (no_movement flow); overwrite with the final values
                # now that the actual exercise message/rationale is known.
                storage_sqlite.update_sent_notification_ai_fields(
                    session.notification_id,
                    exercise_type=session.exercise_type,
                    ai_category=session.ai_payload.get('category'),
                    ai_rationale=session.ai_payload.get('rationale'),
                    reps=session.ai_payload.get('reps'),
                    sets=session.ai_payload.get('sets'),
                    duration_min=session.ai_payload.get('duration_min'),
                    message_text=message_text,
                )
        blocks = []
        if message_text:
            blocks.append({
                'type': 'section',
                'text': {'type': 'mrkdwn', 'text': message_text},
            })
        session.post_stage = 'implemented'
        blocks.extend(build_post_implemented_blocks())
        _update_message(client, blocks, message_text or '運動を実施しましたか？')

    def _send_reason_followup(client):
        if not _claim_followup_display('reason'):
            return
        session.post_stage = 'reason'
        reason_text = ''
        exercise_text = session.ai_payload.get('message') if isinstance(session.ai_payload, dict) else None
        if not isinstance(exercise_text, str) or not exercise_text.strip():
            exercise_text = _fallback_exercise_message(session.exercise_type, session.detection_type)
        blocks = []
        if exercise_text:
            blocks.append({
                'type': 'section',
                'text': {'type': 'mrkdwn', 'text': exercise_text},
            })
        blocks.extend(build_post_reason_blocks())
        _update_message(client, blocks, reason_text)

    def _send_intensity_followup(client):
        if not _claim_followup_display('intensity'):
            return
        session.post_stage = 'intensity'
        intensity_text = ''
        exercise_text = session.ai_payload.get('message') if isinstance(session.ai_payload, dict) else None
        if not isinstance(exercise_text, str) or not exercise_text.strip():
            exercise_text = _fallback_exercise_message(session.exercise_type, session.detection_type)
        blocks = []
        if exercise_text:
            blocks.append({
                'type': 'section',
                'text': {'type': 'mrkdwn', 'text': exercise_text},
            })
        blocks.extend(build_post_intensity_blocks())
        _update_message(client, blocks, intensity_text)

    @app.action('pre_survey_visibility')
    def handle_pre_visibility(ack):
        # Capture selection when user directly interacts with the radio_buttons in the message.
        ack()
        try:
            body = sys._getframe(1).f_locals.get('body') if hasattr(sys, '_getframe') else None
        except Exception:
            body = None
        # If Bolt passes body into closure differently, this handler will also receive body as kwarg —
        # to be robust, ignore here. The actual selection will be parsed in submit handler if missing.
        return

    @app.action('submit_pre_survey')
    def handle_submit_pre_survey(ack, body, client):
        ack()
        if not _get_or_restore_session(body):
            return
        try:
            _sync_session_from_body(body)
        except Exception:
            pass
        visibility = _extract_selected_value(body, 'pre_survey_visibility')

        # Also capture the message ts so we can update the same message
        session.message_ts = body.get('message', {}).get('ts') or body.get('container', {}).get('message_ts')

        if not visibility:
            return
        session.pre_survey['visibility'] = visibility
        # Persist pre-survey answer on the same notification_id before moving forward.
        try:
            if storage_sqlite and session.notification_id:
                storage_sqlite.upsert_response_by_notification_id(
                    notification_id=session.notification_id,
                    response_status='pending',
                    visibility_before=1 if visibility == 'yes' else 0,
                    user_id=session.user_id,
                )
        except Exception:
            try:
                import logging
                logging.exception('Failed to persist pre-survey visibility for notification_id=%s', session.notification_id)
            except Exception:
                pass
        _start_exercise_flow(client, user_visibility=visibility)

    @app.action('post_survey_implemented')
    def handle_post_impl(ack):
        ack()

    @app.action('post_survey_intensity')
    def handle_post_intensity(ack):
        ack()

    @app.action('post_survey_reason')
    def handle_post_reason(ack, body, client):
        ack()
        if not _get_or_restore_session(body):
            return
        try:
            _sync_session_from_body(body)
        except Exception:
            pass
        state_values = body.get('state', {}).get('values', {})
        reason = None
        for actions in state_values.values():
            for action_id, action_data in actions.items():
                if action_id == 'post_survey_reason':
                    selected_opt = action_data.get('selected_option', {})
                    reason = selected_opt.get('value')
        if reason:
            session.post_survey['reason'] = reason

    @app.action('submit_post_implemented')
    def handle_submit_post_implemented(ack, body, client):
        ack()
        if not _get_or_restore_session(body):
            return
        try:
            _sync_session_from_body(body)
        except Exception:
            pass
        implemented = _extract_selected_value(body, 'post_survey_implemented')
        if not implemented:
            return
        session.post_survey['implemented'] = implemented
        if implemented == 'yes':
            if session.exercise_type in ('cardio', 'strength'):
                _send_intensity_followup(client)
            else:
                try:
                    finalize_session_with_retry(client)
                except Exception:
                    try:
                        import logging
                        logging.exception('finalize_session_with_retry failed for user=%s', session.user_id)
                    except Exception:
                        pass
        else:
            _send_reason_followup(client)

    @app.action('submit_post_reason')
    def handle_submit_post_reason(ack, body, client):
        ack()
        if not _get_or_restore_session(body):
            return
        try:
            _sync_session_from_body(body)
        except Exception:
            pass
        reason = _extract_selected_value(body, 'post_survey_reason')
        if not reason:
            return
        session.post_survey['reason'] = reason

        if reason == 'other' and not session.post_survey.get('other_reason'):
            trigger_id = body.get('trigger_id')
            if trigger_id:
                client.views_open(
                    trigger_id=trigger_id,
                    view=build_other_reason_modal(session.notification_id, session.message_ts),
                )
            return

        try:
            finalize_session_with_retry(client)
        except Exception:
            try:
                import logging
                logging.exception('finalize_session_with_retry failed for user=%s', session.user_id)
            except Exception:
                pass

    @app.action('submit_post_intensity')
    def handle_submit_post_intensity(ack, body, client):
        ack()
        if not _get_or_restore_session(body):
            return
        try:
            _sync_session_from_body(body)
        except Exception:
            pass
        intensity = _extract_selected_value(body, 'post_survey_intensity')
        if not intensity:
            return
        session.post_survey['intensity'] = int(intensity)
        try:
            finalize_session_with_retry(client)
        except Exception:
            try:
                import logging
                logging.exception('finalize_session_with_retry failed for user=%s', session.user_id)
            except Exception:
                pass

    @app.view('other_reason_modal')
    def handle_other_reason_view(ack, view, client):
        ack()
        if not _get_or_restore_session_from_view(view):
            return
        try:
            _sync_session_from_body(view)
        except Exception:
            pass
        values = view.get('state', {}).get('values', {})
        other_reason = ''
        for block in values.values():
            for action_data in block.values():
                other_reason = action_data.get('value', '')
        session.post_survey['other_reason'] = other_reason.strip()
        try:
            finalize_session_with_retry(client)
        except Exception:
            try:
                import logging
                logging.exception('finalize_session_with_retry failed for user=%s', session.user_id)
            except Exception:
                pass

    def finalize_session(client):
        implemented = session.post_survey.get('implemented')
        try:
            import logging as _logging
            # core session info for tracing
            try:
                _logging.info('finalize_session start: user=%s channel=%s message_ts=%s notification_id=%s post_stage=%s', session.user_id, session.channel, session.message_ts, session.notification_id, session.post_stage)
            except Exception:
                pass
            try:
                _logging.debug('finalize_session context: pre_survey=%s post_survey=%s ai_context=%s', session.pre_survey, session.post_survey, session.ai_context)
            except Exception:
                pass
        except Exception:
            pass
        if implemented is None:
            return
        reason = session.post_survey.get('reason')
        other_reason = session.post_survey.get('other_reason')
        intensity = session.post_survey.get('intensity')

        if implemented == 'yes':
            response_status = 'implemented'
            implemented_flag = 1
            not_implemented_reason = None
        else:
            response_status = 'not_implemented'
            implemented_flag = 0
            if reason == 'other':
                not_implemented_reason = other_reason or 'other'
            else:
                not_implemented_reason = REASON_LABELS.get(reason or '', reason or 'unknown')

        if storage_sqlite and session.notification_id:
                try:
                    nid_to_update = None
                    try:
                        nid_to_update = str(session.notification_id) if session.notification_id is not None else None
                    except Exception:
                        nid_to_update = session.notification_id
                    try:
                        import logging as _logging
                        _logging.info('Updating response for notification_id=%s (type=%s) user=%s message_ts=%s', nid_to_update, type(nid_to_update), session.user_id, session.message_ts)
                    except Exception:
                        pass
                    # If notification_id is missing, try to find the latest sent notification for this user
                    if not nid_to_update:
                        try:
                            nid_fallback = storage_sqlite.get_latest_sent_notification(session.user_id)
                            if nid_fallback:
                                logging.info('Falling back to latest sent notification_id=%s for user=%s', nid_fallback, session.user_id)
                                nid_to_update = nid_fallback
                        except Exception:
                            logging.exception('Failed to get latest sent notification for fallback')
                    # Call update_response and check whether any row was updated
                    try:
                        # If nid_to_update looks numeric, try to resolve to stored notification_id
                        if nid_to_update and isinstance(nid_to_update, str) and nid_to_update.isdigit():
                            connr = None
                            try:
                                connr = storage_sqlite._get_conn()
                                cur_r = connr.cursor()
                                cur_r.execute('SELECT notification_id FROM notifications WHERE id = %s LIMIT 1', (int(nid_to_update),))
                                rr = cur_r.fetchone()
                                if rr and rr[0]:
                                    try:
                                        import logging as _logging
                                        _logging.info('Resolved numeric notifications.row id %s -> notification_id=%s', nid_to_update, rr[0])
                                    except Exception:
                                        pass
                                    nid_to_update = rr[0]
                            except Exception:
                                try:
                                    import logging as _logging
                                    _logging.exception('Failed resolving numeric notification id for %s', nid_to_update)
                                except Exception:
                                    pass
                            finally:
                                if connr:
                                    try:
                                        connr.close()
                                    except Exception:
                                        pass
                        # If resolution failed or notifications.notification_id was NULL, try fallback to latest sent notification for user
                        try:
                            if nid_to_update and isinstance(nid_to_update, str) and nid_to_update.isdigit():
                                try:
                                    nid_fb = storage_sqlite.get_latest_sent_notification(session.user_id)
                                    if nid_fb:
                                        try:
                                            import logging as _logging
                                            _logging.info('Fallback: using latest sent notification_id=%s for user=%s (was numeric id %s)', nid_fb, session.user_id, nid_to_update)
                                        except Exception:
                                            pass
                                        nid_to_update = nid_fb
                                except Exception:
                                    try:
                                        import logging as _logging
                                        _logging.exception('Failed to get_latest_sent_notification for fallback')
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                    except Exception:
                        pass

                    rows_updated = storage_sqlite.upsert_response_by_notification_id(
                        notification_id=nid_to_update,
                        response_status=response_status,
                        implemented_flag=implemented_flag,
                        not_implemented_reason=not_implemented_reason,
                        visibility_before=1 if session.pre_survey.get('visibility') == 'yes' else 0 if session.pre_survey.get('visibility') == 'no' else None,
                        perceived_exertion=intensity,
                        user_id=session.user_id,
                    )
                    try:
                        import logging as _logging
                        _logging.info('update_response rows_updated=%s for notification_id=%s', rows_updated, nid_to_update)
                    except Exception:
                        pass
                    if not rows_updated:
                        try:
                            import logging as _logging
                            _logging.warning('update_response did not update any rows for notification_id=%s user=%s; will leave pending', nid_to_update, session.user_id)
                        except Exception:
                            pass
                except Exception:
                    try:
                        logging.exception('Failed to call update_response')
                    except Exception:
                        pass

        save_survey_result(
            session.user_id,
            session.exercise_type,
            implemented,
            pre_survey=session.pre_survey,
            post_survey=session.post_survey,
        )

        summary_text = "アンケートを保存しました\nご協力ありがとうございました。"
        summary_blocks = [{
            'type': 'section',
            'text': {'type': 'mrkdwn', 'text': summary_text},
        }]
        # Update the original notification when its message timestamp is known.
        # Otherwise, _update_message posts this completion message to the channel.
        try:
            _update_message(client, summary_blocks, summary_text)
        except Exception:
            # _update_message handles SlackApiError internally; ensure no exception bubbles
            try:
                import logging
                logging.exception('Failed to update final summary message for user=%s', session.user_id)
            except Exception:
                pass

    def finalize_session_with_retry(client, max_attempts: int = 3, backoff_sec: float = 0.5):
        import time as _time
        import logging as _logging
        attempt = 0
        last_exc = None
        while attempt < max_attempts:
            attempt += 1
            try:
                _logging.info('finalize_session attempt %s/%s for user=%s', attempt, max_attempts, session.user_id)
                finalize_session(client)
                _logging.info('finalize_session succeeded on attempt %s for user=%s', attempt, session.user_id)
                return True
            except Exception as e:
                last_exc = e
                try:
                    _logging.exception('finalize_session failed on attempt %s for user=%s', attempt, session.user_id)
                except Exception:
                    pass
                if attempt < max_attempts:
                    _time.sleep(backoff_sec * attempt)
                    continue
        # final failure
        try:
            _logging.error('finalize_session failed after %s attempts for user=%s: %s', max_attempts, session.user_id, str(last_exc))
        except Exception:
            pass
        return False

    return app


def _resolve_exercise_type(cli_arg: Optional[str]) -> str:
    if cli_arg in ('stretch', 'cardio', 'strength'):
        return cli_arg
    return 'stretch'


def launch_session(
    user_id: str,
    channel: Optional[str],
    motion_detected: bool,
    exercise_type: str,
    ai_context: Optional[dict] = None,
    detection_type: Optional[str] = None,
):
    # Prevent launching duplicate interactive sessions for the same user
    try:
        if storage_sqlite:
            try:
                try:
                    conn = storage_sqlite._get_conn()
                    cur = conn.cursor()
                    try:
                        cur.execute("SELECT COUNT(*) FROM received_exercise_responses WHERE user_id=%s AND response_status='pending'", (user_id,))
                        row = cur.fetchone()
                        pending = int(row[0]) if row and row[0] is not None else 0
                    except Exception:
                        pending = 0
                    if pending > 0:
                        print(f"launch_session: pending response exists for {user_id}, skipping new session")
                        return None

                    # Extra guard for duplicated pre-survey posts:
                    # if the same visibility survey was enqueued/sent recently, skip creating another one.
                    # try:
                    #     cur.execute(
                    #         """
                    #         SELECT id
                    #         FROM notifications
                    #         WHERE user_id = %s
                    #           AND text = %s
                    #           AND status IN ('pending', 'reserved', 'processing', 'sent')
                    #           AND created_at >= (NOW() - INTERVAL '15 minutes')
                    #         ORDER BY created_at DESC
                    #         LIMIT 1
                    #         """,
                    #         (user_id, 'アンケート: 人目に付きますか？'),
                    #     )
                    #     recent = cur.fetchone()
                    # except Exception:
                    #     recent = None
                    # if recent:
                    #     try:
                    #         logging.info('launch_session: recent pre-survey exists for user=%s row_id=%s; skipping', user_id, recent[0])
                    #     except Exception:
                    #         pass
                    #     return None
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            except Exception:
                # on any error, fall through to spawning to avoid silent suppression
                pass

    except Exception:
        pass

    args = [
        sys.executable,
        os.path.abspath(__file__),
        '--user-id',
        user_id,
        '--exercise-type',
        _resolve_exercise_type(exercise_type),
    ]
    if channel:
        args.extend(['--channel', channel])
    if motion_detected:
        args.append('--motion-detected')
    if detection_type:
        args.extend(['--detection-type', detection_type])
    if ai_context:
        # Convert datetime-like values safely for subprocess argument serialization.
        def _json_default(o):
            try:
                if hasattr(o, 'isoformat'):
                    return o.isoformat()
            except Exception:
                pass
            try:
                return str(o)
            except Exception:
                return None
        args.extend(['--ai-context-json', json.dumps(ai_context, ensure_ascii=False, default=_json_default)])

    # spawn detached child to avoid inheriting parent's file descriptors where possible
    # Filter env to prevent child from inheriting SLACK_APP_TOKEN (avoids child starting Socket Mode)
    try:
        filtered_env = os.environ.copy()
        filtered_env.pop('SLACK_APP_TOKEN', None)
    except Exception:
        filtered_env = None

    # If a central Socket Mode server is running (USE_SOCKET_SERVER), prefer
    # enqueuing a pre-survey payload so the central server will post the
    # interactive Block Kit and handle responses. This avoids spawning another
    # Socket Mode instance and ensures a single resident handler receives actions.
    try:
        # Accept either explicit env flag or an existing lock file from socket_server.py.
        socket_server_lock = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'locks', 'socket_server.lock')
        socket_server_running = os.path.exists(socket_server_lock)
        if socket_server_running:
            try:
                # create sent notification record and pending response so DB state
                # is consistent with spawn-based flow
                if storage_sqlite:
                    try:
                        logging.info(
                            'launch_session using socket-server enqueue path user=%s lock_exists=%s',
                            user_id,
                            socket_server_running,
                        )
                    except Exception:
                        pass
                    nid = storage_sqlite.create_sent_notification(
                        user_id=user_id,
                        exercise_name=_exercise_name(exercise_type),
                        exercise_type=exercise_type,
                        message_text=_fallback_exercise_message(exercise_type, detection_type),
                        ai_context=ai_context,
                    )
                    storage_sqlite.create_pending_response(nid, user_id)

                    # enqueue pre-survey blocks as payload so central poller posts them
                    try:
                        import json as _json
                        pre_blocks = build_pre_survey_blocks()
                        # include a short fallback text
                        text = 'アンケート: 人目に付きますか？'
                        storage_sqlite.enqueue_notification_for_user(user_id, text, notification_id=nid, payload=_json.dumps(pre_blocks, ensure_ascii=False))
                    except Exception:
                        # if payload enqueue fails, still return the created process-like marker
                        pass
                return None
            except Exception:
                # log and return early: socket-server path was attempted but failed;
                # do NOT fall through to spawn to avoid duplicate notification creation
                try:
                    logging.exception('launch_session: socket-server path failed for user=%s; returning early', user_id)
                except Exception:
                    pass
                return None
    except Exception:
        pass

    # Socket server path was already handled above and returns early.
    # Fall through to spawn-based flow only when socket server is NOT running.
    # Create notification only once via spawn mechanism, do not duplicate via parent enqueue.

    try:
        # On Windows, creationflags can be used to create a new process group
        if os.name == 'nt':
            return subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP, env=filtered_env)
        else:
            return subprocess.Popen(args, start_new_session=True, env=filtered_env)
    except Exception:
        # fallback to basic spawn
        return subprocess.Popen(args, env=filtered_env)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--channel', help='Slack channel ID (override)')
    parser.add_argument('--motion-detected', action='store_true', help='Skip pre-survey when motion was detected')
    parser.add_argument('--exercise-type', choices=['stretch', 'cardio', 'strength'], default='stretch', help='Exercise type to use for the prompt and post-survey')
    parser.add_argument('--user-id', default='seiichirou019@gmail.com', help='User ID to store in DB and JSON')
    parser.add_argument('--detection-type', choices=['motion', 'no_movement'], help='Type of motion detection event')
    parser.add_argument('--serve', action='store_true', help='Run as Socket Mode server (resident)')
    parser.add_argument('--ai-context-json', help='JSON string to pass into the AI prompt context')
    args = parser.parse_args()

    # Load .env if present (simple parser, do not override existing env vars)
    def _load_dotenv(path='.env'):
        try:
            if not os.path.exists(path):
                return
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' not in line:
                        continue
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and os.environ.get(k) is None:
                        os.environ[k] = v
        except Exception:
            pass

    _load_dotenv()

    # bot token: prefer env, otherwise try slack_notifier fallback (module constant)
    bot_token = os.environ.get('SLACK_BOT_TOKEN')
    try:
        import slack_notifier as _sn
        if not bot_token:
            cfg = _sn.get_slack_config()
            if cfg:
                bot_token = cfg[0]
    except Exception:
        pass

    # app_token (Socket Mode) is optional: if missing we fall back to a one-shot post mode
    app_token = os.environ.get('SLACK_APP_TOKEN')

    channel = resolve_channel(args.channel, args.user_id)
    if not channel:
        raise SystemExit('channel not found. Provide --channel or env SLACK_CHANNEL, or ensure tokens_db has mapping for the user')

    if not bot_token:
        raise SystemExit('SLACK_BOT_TOKEN not set and no fallback available')

    ai_context = {}
    if args.ai_context_json:
        try:
            ai_context = json.loads(args.ai_context_json)
        except Exception:
            ai_context = {'raw': args.ai_context_json}

    session = SurveySession(
        user_id=args.user_id,
        channel=channel,
        exercise_type=_resolve_exercise_type(args.exercise_type),
        motion_detected=args.motion_detected,
        detection_type=args.detection_type,
        ai_context=ai_context,
    )

    app = create_app(bot_token, app_token, session)

    # small polling loop to process pending notifications (used by --once and --serve)
    def _process_pending_once(do_send: bool = True, limit: int = 10):
        try:
            rows = storage_sqlite.pop_pending_notifications(limit=limit)
        except Exception as e:
            print('Failed to pop pending notifications:', e)
            return 0
        if not rows:
            return 0
        # rows: list of tuples (id, user_id, channel, text, payload)
        try:
            from slack_sdk import WebClient
        except Exception:
            WebClient = None
        sent_count = 0
        client = WebClient(token=bot_token) if WebClient and bot_token else None
        for r in rows:
            row_id, uid, ch, text, payload = r
            # Idempotency: skip if already marked sent
            # If channel is missing or falsy, mark the notification failed and skip
            if not ch:
                try:
                    print('Skipping send: missing channel for row', row_id, 'user', uid)
                except Exception:
                    pass
                try:
                    storage_sqlite.mark_notification_failed(row_id, reason='missing_channel')
                except Exception:
                    pass
                continue
            try:
                # simple check: if storage reports message_ts or status changed, skip
                # (pop_pending already set status->processing)
                pass
            except Exception:
                pass

            blocks = None
            if payload:
                try:
                    blocks = json.loads(payload)
                    if not isinstance(blocks, list):
                        blocks = None
                except Exception:
                    blocks = None
            text, blocks = slack_notifier.with_channel_mention(text, blocks)

            if not do_send:
                print('Dry-run: would send to', ch, 'text=', text[:120])
                storage_sqlite.mark_notification_sent(row_id, message_ts=None)
                sent_count += 1
                continue

            if not client:
                print('No slack client available; marking failed for row', row_id)
                storage_sqlite.mark_notification_failed(row_id, reason='no_slack_sdk_or_token')
                continue

            # attempt send with basic retry for transient errors
            import time as _time
            max_attempts = 3
            attempt = 0
            while attempt < max_attempts:
                attempt += 1
                try:
                    if blocks:
                        res = client.chat_postMessage(channel=ch, text=(text or ' '), blocks=blocks)
                    else:
                        res = client.chat_postMessage(channel=ch, text=text)
                    # extract ts
                    ts = None
                    try:
                        ts = res.get('ts')
                    except Exception:
                        try:
                            ts = res['ts']
                        except Exception:
                            ts = getattr(res, 'data', {}).get('ts') if getattr(res, 'data', None) else None
                    storage_sqlite.mark_notification_sent(row_id, message_ts=ts)
                    sent_count += 1
                    break
                except Exception as e:
                    err = str(e)
                    # basic handling for rate limit
                    if 'rate_limited' in err.lower() or '429' in err:
                        wait = 5 * attempt
                        print('Rate limited; sleeping', wait)
                        _time.sleep(wait)
                        continue
                    # on last attempt mark failed
                    if attempt >= max_attempts:
                        print('Send failed for row', row_id, 'error:', e)
                        storage_sqlite.mark_notification_failed(row_id, reason=str(e))
                        break
                    else:
                        _time.sleep(0.5 * attempt)
                        continue

        return sent_count

    try:
        # If app_token is present we run in Socket Mode (interactive, resident)
        # Only enable Socket Mode when explicitly requested to avoid accidental
        # multiple Socket Mode instances. Use CLI `--serve` or env `USE_SOCKET_SERVER=1`.
        if app_token and (args.serve or os.environ.get('USE_SOCKET_SERVER') in ('1', 'true', 'True')):
            # start socket mode handler in a background thread while polling
            handler = SocketModeHandler(app, app_token)
            import threading as _threading
            poll_interval = int(os.environ.get('POLL_INTERVAL', '5'))

            # Run the poller in a background thread and run handler.start() in
            # the main thread. SocketModeHandler.start() registers signal
            # handlers and therefore must run in the main thread.
            def _serve_poller():
                try:
                    while True:
                        _process_pending_once(do_send=True, limit=10)
                        import time as _t
                        _t.sleep(poll_interval)
                except Exception:
                    # exit quietly on unexpected errors; main thread controls lifecycle
                    return

            poller_thread = _threading.Thread(target=_serve_poller, daemon=True)
            poller_thread.start()

            print('Started Socket Mode handler; entering serve loop (poller running in background)')
            try:
                # Must call start() in main thread to allow signal handling
                handler.start()
            except KeyboardInterrupt:
                print('Stopped serve loop')
        else:
            # one-shot/legacy behavior: if motion_detected flag set, post single message via client or app
            if args.motion_detected:
                session.ai_payload = _generate_exercise_payload(
                    session.user_id,
                    session.exercise_type,
                    {},
                    session.ai_context,
                    session.detection_type,
                )
                exercise_message = session.ai_payload.get('message') or _fallback_exercise_message(
                    session.exercise_type,
                    session.detection_type,
                )
                if storage_sqlite:
                    session.notification_id = storage_sqlite.create_sent_notification(
                        user_id=session.user_id,
                        exercise_name=_exercise_name(session.exercise_type),
                        exercise_type=session.exercise_type,
                        message_text=exercise_message,
                        ai_category=session.ai_payload.get('category'),
                        ai_rationale=session.ai_payload.get('rationale'),
                        reps=session.ai_payload.get('reps'),
                        sets=session.ai_payload.get('sets'),
                        duration_min=session.ai_payload.get('duration_min'),
                        detection_type=session.detection_type,
                        ai_context=session.ai_context,
                    )
                    storage_sqlite.create_pending_response(session.notification_id, session.user_id)

                # post via app client (if app running) else via WebClient
                try:
                    message_text, blocks = slack_notifier.with_channel_mention(
                        exercise_message,
                        build_post_survey_blocks(session.exercise_type),
                    )
                    sent = app.client.chat_postMessage(channel=session.channel, text=message_text, blocks=blocks)
                    session.message_ts = sent.get('ts') if hasattr(sent, 'get') else None
                except Exception:
                    try:
                        from slack_sdk import WebClient
                        client = WebClient(token=bot_token)
                        sent = client.chat_postMessage(channel=session.channel, text=message_text, blocks=blocks)
                        session.message_ts = sent.get('ts') if hasattr(sent, 'get') else None
                    except Exception:
                        print('Failed to send exercise message in one-shot mode')
            else:
                # When not motion_detected and not serving, process pending once if any
                _count = _process_pending_once(do_send=True, limit=10)
                print('Processed pending count=', _count)
    except KeyboardInterrupt:
        print('Stopped')


if __name__ == '__main__':
    main()
