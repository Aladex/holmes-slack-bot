import os
import logging
import threading
from collections import defaultdict

import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("holmes-slack-bot")

HOLMES_URL = os.environ.get("HOLMES_URL", "http://holmes-holmes.monitoring.svc.cluster.local")
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
ADDITIONAL_SYSTEM_PROMPT = os.environ.get(
    "ADDITIONAL_SYSTEM_PROMPT",
    "You are an SRE AI assistant in a Slack workspace. "
    "Write ALL output in Russian language. Use plain text, no markdown headers. "
    "Use bullet points sparingly. "
    "Do NOT use the prometheus toolset for metrics queries - it is broken. "
    "Instead use bash with curl. "
    "IMPORTANT: Always wrap ALL curl arguments in single quotes. "
    "Always pass timeout as integer, not string. "
    "For metrics: curl -sG "
    "'http://vmselect-victoria-metrics-k8s-stack.monitoring.svc:8481/select/0/prometheus/api/v1/query' "
    "--data-urlencode 'query=YOUR_PROMQL' | jq .data.result "
    "— For logs from legacy servers use VictoriaLogs "
    "(labels: host, app_type, name, source_type): "
    "curl -sG 'http://victoria-logs-cluster-vlselect.victoria-logs-cluster.svc:9471/select/logsql/query' "
    "--data-urlencode 'query=_stream:{host=\"HOSTNAME\"} AND error' "
    "--data-urlencode 'limit=50' --data-urlencode 'start=-1h' "
    "— For Kubernetes pod logs use kubectl logs.",
)

app = App(token=SLACK_BOT_TOKEN)

# Per-thread conversation history: {(channel, thread_ts): [messages]}
conversations = defaultdict(list)
conversations_lock = threading.Lock()

MAX_HISTORY = 50  # max messages per thread


def call_holmes(ask: str, conversation_history: list) -> str:
    """Call Holmes /api/chat and return the analysis text."""
    payload = {
        "ask": ask,
        "additional_system_prompt": ADDITIONAL_SYSTEM_PROMPT,
    }
    if conversation_history:
        payload["conversation_history"] = conversation_history

    try:
        resp = requests.post(
            f"{HOLMES_URL}/api/chat",
            json=payload,
            timeout=300,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("analysis", "No response from Holmes.")
    except requests.exceptions.Timeout:
        return "Holmes investigation timed out. Try a more specific question."
    except requests.exceptions.RequestException as e:
        logger.error(f"Holmes API error: {e}")
        return f"Error connecting to Holmes: {e}"


def get_thread_key(channel: str, thread_ts: str) -> tuple:
    return (channel, thread_ts)


def handle_message(event, say, bot_user_id: str):
    """Process a message that mentions the bot or is in an active thread."""
    text = event.get("text", "")
    channel = event["channel"]
    thread_ts = event.get("thread_ts", event["ts"])
    user = event.get("user", "unknown")

    # Strip bot mention from text
    clean_text = text.replace(f"<@{bot_user_id}>", "").strip()
    if not clean_text:
        return

    thread_key = get_thread_key(channel, thread_ts)

    with conversations_lock:
        history = conversations[thread_key]
        history.append({"role": "user", "content": clean_text})
        # Trim old messages (keep system message + last N)
        if len(history) > MAX_HISTORY:
            conversations[thread_key] = [history[0]] + history[-(MAX_HISTORY - 1):]
        history_snapshot = list(conversations[thread_key])
        # Holmes requires conversation_history to start with system message
        if not history_snapshot or history_snapshot[0].get("role") != "system":
            history_snapshot.insert(0, {"role": "system", "content": ADDITIONAL_SYSTEM_PROMPT})

    # Post a "thinking" message
    thinking = say(text="Investigating...", thread_ts=thread_ts)

    # Call Holmes
    answer = call_holmes(clean_text, history_snapshot)

    # Update thinking message with the answer
    try:
        app.client.chat_update(
            channel=channel,
            ts=thinking["ts"],
            text=answer,
        )
    except Exception:
        # Fallback: post new message
        say(text=answer, thread_ts=thread_ts)

    # Save assistant response to history
    with conversations_lock:
        conversations[thread_key].append({"role": "assistant", "content": answer})


@app.event("app_mention")
def handle_mention(event, say):
    """Respond when someone mentions @Holmes in a channel."""
    bot_user_id = app.client.auth_test()["user_id"]
    handle_message(event, say, bot_user_id)


@app.event("message")
def handle_thread_reply(event, say):
    """Respond to messages in threads where bot is already participating."""
    # Skip bot's own messages
    if event.get("bot_id") or event.get("subtype"):
        return

    # Only respond in threads
    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return

    channel = event["channel"]
    thread_key = get_thread_key(channel, thread_ts)

    # Check if we have conversation history for this thread
    with conversations_lock:
        if thread_key not in conversations:
            return

    bot_user_id = app.client.auth_test()["user_id"]
    handle_message(event, say, bot_user_id)


if __name__ == "__main__":
    logger.info(f"Starting Holmes Slack Bot, Holmes URL: {HOLMES_URL}")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()