# Backend container. The frontend is static and goes to Vercel — see DEPLOY.md.
FROM python:3.12-slim

# Scheduling is computed in IST explicitly, so this only aligns the platform's
# own log timestamps with the app's. Worth it when debugging a missed run.
ENV TZ=Asia/Kolkata \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# signals.db holds the entire track record. Mount a volume here or every
# deploy wipes the outcome history the memory system depends on.
VOLUME ["/data"]
ENV DB_DIR=/data

ENV HOST=0.0.0.0 \
    PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os; \
        urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health').read()"

CMD ["python", "app.py"]
