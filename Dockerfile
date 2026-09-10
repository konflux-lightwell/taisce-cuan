# Stage 1: build the wheel
FROM registry.access.redhat.com/ubi10/python-312-minimal@sha256:ce5c47b6bc9756685726b5a503729920ba30fe89b82fc7d63b6103a2dd02c734 as builder

USER 0
WORKDIR /build

COPY pyproject.toml README.md ./
COPY src/ src/

RUN mkdir /venv && chown -R 1001:0 /build /venv
USER 1001

RUN python3.12 -m venv /venv && \
    /venv/bin/pip install . --no-cache-dir

# Stage 2: runtime image with Git and standard toolchain
FROM registry.access.redhat.com/ubi10/python-312-minimal@sha256:ce5c47b6bc9756685726b5a503729920ba30fe89b82fc7d63b6103a2dd02c734

ARG GIT_COMMIT_SHA=""
ENV GIT_COMMIT_SHA=${GIT_COMMIT_SHA}

USER 0
RUN microdnf install -y git tar gzip && microdnf clean all

COPY --from=builder /venv /venv

ENV PATH="/venv/bin:$PATH" \
    HOME="/tekton/home"

USER 1001

ENTRYPOINT ["/venv/bin/taisce-cuan"]
