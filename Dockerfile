FROM ghcr.io/osgeo/gdal:ubuntu-small-3.12.0

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -c "from osgeo import gdal; assert int(gdal.VersionInfo('VERSION_NUM')) >= 3120000; assert gdal.Algorithm('raster', 'tile') is not None"

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY app ./app
COPY scripts/process_imagery.py ./scripts/process_imagery.py
COPY scripts/verify_gdal_python_pipeline.py ./scripts/verify_gdal_python_pipeline.py
COPY pyproject.toml .

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
