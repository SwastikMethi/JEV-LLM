FROM python:3.13-slim

# Hugging Face Spaces run the container as UID 1000.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR $HOME/app

COPY --chown=user requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user app app
COPY --chown=user data data

# One worker only: demo sessions live in this process's memory and reset on restart.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
