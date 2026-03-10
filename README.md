# Holmes Slack Bot

Slack bot proxy for [HolmesGPT](https://github.com/HolmesGPT/holmesgpt) `/api/chat` — ask your SRE AI agent questions directly from Slack.

## How it works

- Mention `@Holmes` in any channel to start an investigation
- Continue the conversation in the thread — the bot remembers context
- Uses Holmes `/api/chat` with full tool calling (kubectl, bash, runbooks, etc.)

## Slack App Setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From Scratch
2. **Socket Mode**: Enable, create an App-Level Token with `connections:write` scope → save as `SLACK_APP_TOKEN`
3. **OAuth & Permissions**: Add Bot Token Scopes:
   - `app_mentions:read`
   - `chat:write`
   - `channels:history` (for thread replies)
   - `groups:history` (for private channels)
4. **Event Subscriptions**: Enable, subscribe to:
   - `app_mention`
   - `message.channels`
   - `message.groups`
5. Install to workspace → copy Bot User OAuth Token → save as `SLACK_BOT_TOKEN`

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SLACK_BOT_TOKEN` | yes | Bot User OAuth Token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | yes | App-Level Token (`xapp-...`) |
| `HOLMES_URL` | no | Holmes server URL (default: `http://holmes-holmes.monitoring.svc.cluster.local`) |
| `ADDITIONAL_SYSTEM_PROMPT` | no | Custom system prompt for the bot |
| `LOG_LEVEL` | no | Logging level (default: `INFO`) |

## Run locally

```bash
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_APP_TOKEN=xapp-...
export HOLMES_URL=http://localhost:8080
pip install -r requirements.txt
python main.py
```

## Deploy to Kubernetes

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: holmes-slack-bot
spec:
  replicas: 1
  selector:
    matchLabels:
      app: holmes-slack-bot
  template:
    metadata:
      labels:
        app: holmes-slack-bot
    spec:
      containers:
        - name: bot
          image: ghcr.io/aladex/holmes-slack-bot:main
          env:
            - name: SLACK_BOT_TOKEN
              valueFrom:
                secretKeyRef:
                  name: holmes-slack-bot
                  key: slack-bot-token
            - name: SLACK_APP_TOKEN
              valueFrom:
                secretKeyRef:
                  name: holmes-slack-bot
                  key: slack-app-token
            - name: HOLMES_URL
              value: "http://holmes-holmes.monitoring.svc.cluster.local"
```