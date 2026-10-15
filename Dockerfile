FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN groupadd --system taskforge && useradd --system --gid taskforge --home /app taskforge
COPY pyproject.toml requirements.lock ./
COPY taskforge ./taskforge
COPY manage.py ./
RUN pip install -c requirements.lock '.[observability]' && python manage.py collectstatic --noinput && chown -R taskforge:taskforge /app
USER taskforge
EXPOSE 8000
CMD ["gunicorn", "taskforge.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-"]
