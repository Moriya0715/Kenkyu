"""OpenAI helper functions for generating short Slack notification messages."""

import json
import logging
import os
from typing import Any, Dict, Optional

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

_client = None
_env_loaded = False
_knowledge_cache = None


def _knowledge_dir() -> str:
    """Return the directory that stores prompt knowledge files."""
    return os.environ.get(
        'OPENAI_KNOWLEDGE_DIR',
        os.path.join(os.path.dirname(__file__), 'ai_knowledge'),
    )


def _load_project_env() -> None:
    """Load .env from project root once (without external dependencies)."""
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True

    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if not os.path.exists(env_path):
        logging.warning('.env file not found at %s', env_path)
        return

    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
                if key == 'OPENAI_API_KEY' and value:
                    logging.info('Loaded OPENAI_API_KEY from .env')
    except Exception:
        logging.exception('Failed to load .env file')


def _get_client() -> Optional[Any]:
    """Create and cache OpenAI client if API key and SDK are available."""
    global _client
    _load_project_env()
    if _client is not None:
        return _client
    if OpenAI is None:
        return None
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        logging.error('OPENAI_API_KEY not found in environment or .env; cannot initialize OpenAI client')
        return None
    try:
        _client = OpenAI(api_key=api_key)
        return _client
    except Exception:
        logging.exception('Failed to initialize OpenAI client')
        return None


def _extract_text_from_response(response: Any) -> str:
    """Extract text from OpenAI Responses API result robustly."""
    try:
        text = getattr(response, 'output_text', None)
        if isinstance(text, str) and text.strip():
            return text.strip()
    except Exception:
        pass

    try:
        out = getattr(response, 'output', None)
        if isinstance(out, list):
            chunks = []
            for item in out:
                content = getattr(item, 'content', None)
                if isinstance(content, list):
                    for c in content:
                        t = getattr(c, 'text', None)
                        if isinstance(t, str) and t.strip():
                            chunks.append(t.strip())
            if chunks:
                return '\n'.join(chunks)
    except Exception:
        pass
    return ''


def _load_text_file(path: str) -> str:
    """Load a UTF-8 text file if it exists."""
    if not os.path.exists(path):
        return ''
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        logging.exception('Failed to load knowledge file: %s', path)
        return ''


def _load_knowledge_bundle() -> Dict[str, str]:
    """Load common and category-specific knowledge text once."""
    global _knowledge_cache
    if _knowledge_cache is not None:
        return _knowledge_cache

    base_dir = _knowledge_dir()
    bundle = {
        'common': _load_text_file(os.path.join(base_dir, 'common.md')),
        'strength': _load_text_file(os.path.join(base_dir, 'strength.md')),
        'cardio': _load_text_file(os.path.join(base_dir, 'cardio.md')),
        'stretch': _load_text_file(os.path.join(base_dir, 'stretch.md')),
    }
    _knowledge_cache = bundle
    return bundle


def _load_knowledge_bundle_filtered(detection_type: Optional[str]) -> Dict[str, str]:
    """Load knowledge bundle filtered by detection type.
    
    - 'no_movement': common + stretch + strength のみ（cardio 削除）
    - 'motion': common + cardio + strength のみ（stretch 削除）
    - None/その他: 全て（互換性維持）
    """
    base_knowledge = _load_knowledge_bundle()
    
    if detection_type == 'no_movement':
        # 無動作時：ストレッチと筋トレのみ
        return {
            'common': base_knowledge.get('common', ''),
            'stretch': base_knowledge.get('stretch', ''),
            'strength': base_knowledge.get('strength', ''),
        }
    elif detection_type == 'motion':
        # 動作検知時：有酸素と筋トレのみ
        return {
            'common': base_knowledge.get('common', ''),
            'cardio': base_knowledge.get('cardio', ''),
            'strength': base_knowledge.get('strength', ''),
        }
    else:
        # デフォルト（全て）
        return base_knowledge


def _format_context(context: Dict[str, Any]) -> str:
    """Serialize context into stable JSON for the prompt."""
    try:
        return json.dumps(context, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(context)


def _build_prompt(event_type: str, user_id: str, context: Dict[str, Any]) -> str:
    """Build a prompt that lets the model choose between exercise categories based on detection_type."""
    detection_type = context.get('detection_type')
    knowledge = _load_knowledge_bundle_filtered(detection_type)
    context_text = _format_context(context)
    
    # Set instruction text based on detection_type
    if detection_type == 'no_movement':
        exercise_instruction = 'ストレッチまたは筋トレのうち最も適切な方向を1つ選び'
        exercise_types = 'stretch, strength のいずれかにしてください。'
        knowledge_sections = f'''
        ## 筋トレ知識
        {knowledge.get("strength", "")}
        ## ストレッチ知識
        {knowledge.get("stretch", "")}
'''
    elif detection_type == 'motion':
        exercise_instruction = '有酸素運動または筋トレのうち最も適切な方向を1つ選び'
        exercise_types = 'cardio, strength のいずれかにしてください。'
        knowledge_sections = f'''
        ## 有酸素運動知識
        {knowledge.get("cardio", "")}
        ## 筋トレ知識
        {knowledge.get("strength", "")}
'''
    else:
        # Default behavior for backward compatibility
        exercise_instruction = '筋トレ・有酸素運動・ストレッチのうち最も適切な方向を1つ選び'
        exercise_types = 'strength, cardio, stretch のいずれかにしてください。'
        knowledge_sections = f'''
        ## 筋トレ知識
        {knowledge.get("strength", "")}
        ## 有酸素運動知識
        {knowledge.get("cardio", "")}
        ## ストレッチ知識
        {knowledge.get("stretch", "")}
'''

    if detection_type == 'no_movement':
        exercise_type_rule = 'context.exercise_type には固定されず、recommended_types に列挙された候補（stretch / strength）を比較して最適な1つを選んでください。'
    else:
        exercise_type_rule = 'context.exercise_type には固定されず、recommended_types に列挙された候補（cardio / strength）を比較して最適な1つを選んでください。'
    
    return (
        'あなたは健康行動を促すSlack通知文の作成アシスタントです。\n'
        f'入力された状況と知識ファイルを踏まえ、{exercise_instruction}、通知文を作成してください。\n'
        'context には運動名、運動種別、事前アンケート、過去の送信履歴、種目別実施率、今週の種目別実施回数などの要約が入ります。\n'
        '心拍数や歩数などの生体データは context に含まれない前提です。そこを補完しないでください。\n'
        f'{exercise_type_rule}\n'
        '改行を必ず使い、見やすい複数行の文章にしてください。絵文字は使っても構いません。\n'
        '出力は必ずJSON文字列で返してください。コードフェンスは使わないでください。キーは category, message, rationale, reps, sets, duration_min の6つです。\n'
        f'category は {exercise_types}\n'
        'rationale は内部確認用の短い説明で、20文字程度にすること。\n'
        'reps は「1セットあたりに行う回数」、duration_min は「運動時の姿勢を保つのに必要な継続時間（秒）」を表す別々の数値です。提示する運動内容に応じて適切に埋めること。\n'
        'reps、duration_min、のどちらか、もしくは両方に必ず具体的な整数値を埋めること。\n'
        'setsは必ず具体的な整数値を埋めてください（null にしないこと）。\n'
        '例えばmessage本文に「回数の目安：10回」とだけ書く場合でも、これは1セットあたりの回数を意味するので reps=10 とし、セット数が本文に明記されていなければ一般的な目安（例：sets=1〜3）を補って必ず数値を入れてください。\n'
        'ストレッチなどセットの概念が薄い種目でも、sets=1 として扱い、reps(回数)もしくは、duration_min に継続時間（秒）を入れてください。また、reps、duration_min のどちらの値も埋めていいとすること。\n'

        '## 共通ルール\n'
        f'{knowledge.get("common", "")}\n\n'
        f'{knowledge_sections}'
        '## 入力\n'
        f'event_type: {event_type}\n'
        f'user_id: {user_id}\n'
        f'context: {context_text}\n'
    )


def _extract_message_from_output(raw_text: str, fallback_message: Optional[str] = None) -> str:
    """Return a clean message from either JSON or plain text output."""
    if not raw_text:
        return fallback_message or ''

    text = raw_text.strip()
    if not text:
        return fallback_message or ''

    if text.startswith('```'):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith('```'):
            text = '\n'.join(lines[1:-1]).strip()

    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            message = payload.get('message') or payload.get('msg')
            if isinstance(message, str) and message.strip():
                return message.strip()
    except Exception:
        pass

    return text if text else (fallback_message or '')


def _extract_int_or_none(value: Any) -> Optional[int]:
    """Best-effort conversion to int; returns None when the value is missing or invalid."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_payload_from_output(raw_text: str, fallback_message: Optional[str] = None) -> Dict[str, Any]:
    """Return a structured payload from JSON or plain text output."""
    payload = {
        'category': None,
        'message': fallback_message or '',
        'rationale': '',
        'reps': None,
        'sets': None,
        'duration_min': None,
    }
    if not raw_text:
        return payload

    text = raw_text.strip()
    if not text:
        return payload

    if text.startswith('```'):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith('```'):
            text = '\n'.join(lines[1:-1]).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            category = parsed.get('category')
            if isinstance(category, str) and category.strip():
                payload['category'] = category.strip()
            message = parsed.get('message') or parsed.get('msg')
            if isinstance(message, str) and message.strip():
                payload['message'] = message.strip()
            rationale = parsed.get('rationale')
            if isinstance(rationale, str) and rationale.strip():
                payload['rationale'] = rationale.strip()
            payload['reps'] = _extract_int_or_none(parsed.get('reps'))
            payload['sets'] = _extract_int_or_none(parsed.get('sets'))
            payload['duration_min'] = _extract_int_or_none(parsed.get('duration_min'))
            return payload
    except Exception:
        pass

    payload['message'] = _extract_message_from_output(raw_text, fallback_message=fallback_message)
    return payload


# def generate_notification_message(
#     event_type: str,
#     user_id: str,
#     context: Dict[str, Any],
#     fallback_message: Optional[str] = None,
# ) -> str:
#     """Generate a Slack notification message using OpenAI.

#     If OpenAI is unavailable or fails, returns fallback_message (or empty string).
#     """
#     client = _get_client()
#     if client is None:
#         return fallback_message or ''

#     model = os.environ.get('OPENAI_NOTIFICATION_MODEL', 'gpt-4o-mini')
#     prompt = _build_prompt(event_type, user_id, context)

#     try:
#         response = client.responses.create(model=model, input=prompt)
#         msg = _extract_text_from_response(response)
#         if not msg:
#             return fallback_message or ''
#         return _extract_message_from_output(msg, fallback_message=fallback_message)
#     except Exception:
#         logging.exception('OpenAI notification generation failed')
#         return fallback_message or ''


def generate_notification_payload(
    event_type: str,
    user_id: str,
    context: Dict[str, Any],
    fallback_message: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a Slack notification payload using OpenAI."""
    client = _get_client()
    if client is None:
        return {'category': None, 'message': fallback_message or '', 'rationale': '', 'reps': None, 'sets': None, 'duration_min': None}

    model = os.environ.get('OPENAI_NOTIFICATION_MODEL', 'gpt-4o-mini')
    prompt = _build_prompt(event_type, user_id, context)

    try:
        response = client.responses.create(model=model, input=prompt)
        msg = _extract_text_from_response(response)
        if not msg:
            return {'category': None, 'message': fallback_message or '', 'rationale': '', 'reps': None, 'sets': None, 'duration_min': None}
        return _extract_payload_from_output(msg, fallback_message=fallback_message)
    except Exception:
        logging.exception('OpenAI notification payload generation failed')
        return {'category': None, 'message': fallback_message or '', 'rationale': '', 'reps': None, 'sets': None, 'duration_min': None}

"""
if __name__ == '__main__':
    sample = generate_notification_message(
        event_type='increase',
        user_id='example@example.com',
        context={'prev_steps': 1200, 'curr_steps': 1255, 'diff': 55, 'ts': '2026-04-11T09:01:00+09:00'},
        fallback_message='歩数が増えました。軽くストレッチしてみましょう。',
    )
    print(sample)
"""