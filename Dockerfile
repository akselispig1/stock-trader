# Runs the always-on trading loop + dashboard. Works on any container host
# (Railway, Fly, a VPS) - the same process the Render blueprint starts.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/ ./bot/
COPY docs/ ./docs/

ENV PORT=8080
EXPOSE 8080
CMD ["python", "-m", "bot.serve"]
