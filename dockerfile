# Etapa 1: Compilación nativa con Nuitka
FROM python:3.13-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ patchelf ccache libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt nuitka

COPY config.py db.py excel_engine.py main.py ./

# Compilar módulos internos como extensiones binarias y main.py como ejecutable standalone
RUN python -m nuitka --module config.py --output-dir=/dist \
    && python -m nuitka --module db.py --output-dir=/dist \
    && python -m nuitka --module excel_engine.py --output-dir=/dist \
    && python -m nuitka --standalone --follow-imports \
       --include-module=config --include-module=db --include-module=excel_engine \
       --output-dir=/dist main.py

# Etapa 2: Imagen de producción limpia (soporta servicio API compilado o Streamlit Admin)
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_MODE=api \
    PORT=8081

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiar binario compilado de main.py, app_admin.py (para el servicio admin en Railway) y estáticos
COPY --from=builder /dist/main.dist /app/main.dist/
COPY app_admin.py config.py db.py excel_engine.py ./
COPY static /app/static

EXPOSE 8081

CMD ["sh", "-c", "if [ \"$APP_MODE\" = \"admin\" ]; then exec streamlit run app_admin.py --server.address 0.0.0.0 --server.port ${PORT:-8501}; else exec /app/main.dist/main.bin; fi"]