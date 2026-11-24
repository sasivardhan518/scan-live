FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080
RUN useradd -m -u 1000 user
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir yfinance pandas

COPY --chown=user . .
USER user

EXPOSE 8080
CMD ["python", "-u", "server.py", "--provider", "yahoo"]
