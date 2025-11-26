FROM python:3.12-alpine

# Build-time arg, avec valeur par défaut pour les builds locaux
ARG GIT_COMMIT=dev

# Réduire les writes & la verbosité de pip
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# User non-root minimal
RUN adduser -D -u 1000 watcher

WORKDIR /app

# Copier uniquement le strict nécessaire
COPY src/watcher.py /app/watcher.py

# Installer la seule dépendance dont on a besoin
RUN pip install --no-cache-dir requests

USER watcher

ENTRYPOINT ["python", "-u", "/app/watcher.py"]
