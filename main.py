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
HOLMES_TIMEOUT = int(os.environ.get("HOLMES_TIMEOUT", "300"))
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
ADDITIONAL_SYSTEM_PROMPT = os.environ.get(
    "ADDITIONAL_SYSTEM_PROMPT",
    "You are an SRE AI assistant in a Slack workspace. "
    "Write concise answers in the same language as the user's question. "
    "Use plain text, no markdown headers. Use bullet points sparingly.",
)

AUTO_INVESTIGATE = os.environ.get("AUTO_INVESTIGATE", "false").lower() == "true"
ALERT_KEYWORDS = ["FIRING", "RESOLVED"]

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
            timeout=HOLMES_TIMEOUT,
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


def get_event_text(event):
    """Extract text from event, including attachments (Alertmanager uses attachments)."""
    text = event.get("text", "")
    if not text:
        for att in event.get("attachments", []):
            fallback = att.get("fallback", "")
            if fallback:
                return fallback
            title = att.get("title", "")
            if title:
                return title
    return text


def get_alert_full_text(event):
    """Extract full alert text from attachments for investigation."""
    parts = []
    for att in event.get("attachments", []):
        if att.get("title"):
            parts.append(att["title"])
        if att.get("text"):
            parts.append(att["text"])
    return "\n".join(parts) if parts else event.get("text", "")


def is_alert_message(event):
    """Check if message looks like an alert from Alertmanager."""
    if not event.get("bot_id"):
        return False
    text = get_event_text(event)
    return any(kw in text for kw in ALERT_KEYWORDS)


@app.event("message")
def handle_thread_reply(event, say):
    """Respond to thread replies and auto-investigate alerts."""
    logger.debug("Message event: subtype=%s bot_id=%s channel=%s text=%s",
                 event.get("subtype"), event.get("bot_id"),
                 event.get("channel"), (event.get("text") or "")[:120])
    bot_user_id = app.client.auth_test()["user_id"]

    # Auto-investigate alert messages from bots (e.g. Alertmanager)
    if AUTO_INVESTIGATE and is_alert_message(event):
        # Skip RESOLVED alerts
        alert_text = get_event_text(event)
        if "RESOLVED" in alert_text:
            return
        # Inject full alert text into event for handle_message
        event["text"] = get_alert_full_text(event)
        logger.info("Auto-investigating alert: %s", alert_text[:100])
        handle_message(event, say, bot_user_id)
        return

    # Skip bot messages for normal flow
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

    handle_message(event, say, bot_user_id)


if __name__ == "__main__":
    logger.info(f"Starting Holmes Slack Bot, Holmes URL: {HOLMES_URL}")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()