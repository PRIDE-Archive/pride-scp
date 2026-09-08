# syntax=docker/dockerfile:1.7
ARG RUST_BASE_IMAGE=rust:1.80.1-bookworm
ARG RUNTIME_BASE_IMAGE=ubuntu:24.04
ARG OLLAMA_IMAGE=ollama/ollama:0.33.3
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.10

FROM ${RUST_BASE_IMAGE} AS rust-builder
WORKDIR /src
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY crates ./crates
RUN cargo build --release --locked -p pride-scp-cli
RUN { \
      echo "rustc_version=$(rustc --version)"; \
      echo "cargo_version=$(cargo --version)"; \
      echo "cargo_lock_sha256=$(sha256sum Cargo.lock | awk '{print $1}')"; \
    } > /tmp/rust-build-info.txt

FROM ${UV_IMAGE} AS uv-bin

FROM ${RUNTIME_BASE_IMAGE} AS python-builder
ARG PYTHON_VERSION=3.12.12
ENV DEBIAN_FRONTEND=noninteractive \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=uv-bin /uv /usr/local/bin/uv
COPY python/requirements.lock /tmp/requirements.lock
RUN uv python install "${PYTHON_VERSION}" \
 && uv venv --python "${PYTHON_VERSION}" /opt/pride-scp/venv \
 && uv pip install --python /opt/pride-scp/venv/bin/python --no-cache -r /tmp/requirements.lock \
 && /opt/pride-scp/venv/bin/python -m compileall -q /opt/pride-scp/venv \
 && rm -rf /tmp/uv-cache
RUN { \
      echo "python_version=$(/opt/pride-scp/venv/bin/python --version 2>&1)"; \
      echo "uv_version=$(uv --version)"; \
      echo "python_package_lock_sha256=$(sha256sum /tmp/requirements.lock | awk '{print $1}')"; \
    } > /tmp/python-build-info.txt

FROM ${OLLAMA_IMAGE} AS ollama-model-builder
ARG PRIDE_SCP_BAKE_OLLAMA_MODEL=0
ARG PRIDE_SCP_OLLAMA_MODEL=qwen2.5:3b
RUN mkdir -p /root/.ollama \
 && if [ "${PRIDE_SCP_BAKE_OLLAMA_MODEL}" = "1" ]; then \
      ollama serve >/tmp/ollama-build.log 2>&1 & pid=$!; \
      ready=0; \
      for i in $(seq 1 60); do ollama list >/dev/null 2>&1 && ready=1 && break; sleep 1; done; \
      if [ "$ready" != "1" ]; then cat /tmp/ollama-build.log >&2; kill "$pid" || true; exit 2; fi; \
      ollama pull "${PRIDE_SCP_OLLAMA_MODEL}"; \
      kill "$pid" || true; wait "$pid" || true; \
    fi

FROM ${OLLAMA_IMAGE} AS ollama-runtime

FROM ${RUNTIME_BASE_IMAGE} AS runtime
ARG PROJECT_VERSION=unknown
ARG VCS_REF=unknown
ARG SOURCE_STATE=unknown
ARG SOURCE_FINGERPRINT=unknown
ARG IMAGE_BUILD_ID=unknown
ARG BUILD_TIMESTAMP=unknown
ARG RUST_BASE_IMAGE=unknown
ARG RUNTIME_BASE_IMAGE=unknown
ARG OLLAMA_IMAGE=unknown
ARG UV_IMAGE=unknown
ARG PRIDE_SCP_BAKE_OLLAMA_MODEL=0
ARG PRIDE_SCP_OLLAMA_MODEL=qwen2.5:3b

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      libopenblas0 \
      libvulkan1 \
      p7zip-full \
      poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ollama-runtime /usr/bin/ollama /usr/bin/ollama
COPY --from=ollama-runtime /usr/lib/ollama /usr/lib/ollama
COPY --from=ollama-model-builder /root/.ollama /opt/pride-scp/baked-ollama-models
COPY --from=rust-builder /src/target/release/pride-scp /usr/local/bin/pride-scp
COPY --from=rust-builder /tmp/rust-build-info.txt /tmp/rust-build-info.txt
COPY --from=python-builder /opt/python /opt/python
COPY --from=python-builder /opt/pride-scp/venv /opt/pride-scp/venv
COPY --from=python-builder /tmp/python-build-info.txt /tmp/python-build-info.txt

WORKDIR /opt/pride-scp
COPY Cargo.toml Cargo.lock rust-toolchain.toml README.md LICENSE ./
COPY config ./config
COPY resources ./resources
COPY docs ./docs
COPY python ./python
COPY scripts ./scripts
COPY containers/run_with_ollama.sh /usr/local/bin/pride-scp-with-ollama

ENV PATH="/opt/pride-scp/venv/bin:/usr/local/bin:/usr/bin:/bin" \
    PYTHONPATH="/opt/pride-scp/scripts:/opt/pride-scp/python/stages:/opt/pride-scp/python:/opt/pride-scp" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    TOKENIZERS_PARALLELISM=false \
    LD_LIBRARY_PATH="/usr/lib/ollama:/usr/lib/ollama/cuda_v12:/usr/lib/ollama/cuda_v13:/usr/local/nvidia/lib:/usr/local/nvidia/lib64" \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    NVIDIA_VISIBLE_DEVICES=all \
    OLLAMA_NO_CLOUD=1 \
    PRIDE_SCP_OLLAMA_MODEL="${PRIDE_SCP_OLLAMA_MODEL}"

RUN mkdir -p /opt/pride-scp /data /models /cache /results /scratch \
 && { \
      echo "project=pride-scp"; \
      echo "project_version=${PROJECT_VERSION}"; \
      echo "vcs_ref=${VCS_REF}"; \
      echo "source_state=${SOURCE_STATE}"; \
      echo "source_fingerprint=${SOURCE_FINGERPRINT}"; \
      echo "image_build_id=${IMAGE_BUILD_ID}"; \
      echo "build_timestamp=${BUILD_TIMESTAMP}"; \
      echo "rust_base_image=${RUST_BASE_IMAGE}"; \
      echo "runtime_base_image=${RUNTIME_BASE_IMAGE}"; \
      echo "ollama_image=${OLLAMA_IMAGE}"; \
      echo "uv_image=${UV_IMAGE}"; \
      echo "ollama_model_baked=${PRIDE_SCP_BAKE_OLLAMA_MODEL}"; \
      echo "ollama_model=${PRIDE_SCP_OLLAMA_MODEL}"; \
      cat /tmp/rust-build-info.txt; \
      cat /tmp/python-build-info.txt; \
      echo "runtime_ollama_version=$(ollama --version 2>&1 | head -1)"; \
    } > /opt/pride-scp/build-info.txt \
 && python -c 'import requests, pymupdf, pypdf; print("python runtime imports: OK")' \
 && pride-scp --version

LABEL org.opencontainers.image.title="PRIDE-SCP" \
      org.opencontainers.image.version="${PROJECT_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_TIMESTAMP}" \
      org.opencontainers.image.description="PRIDE-SCP Rust/Python scientific pipeline with optional offline Ollama runtime"

CMD ["pride-scp", "--help"]
