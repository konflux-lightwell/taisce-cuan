# Stage 1: build the wheel
FROM registry.access.redhat.com/ubi10/python-312-minimal@sha256:3f3c6dda26caa5b2200fba25721c7a970b1acd4677bbc9865bf25dce62da918a as builder

USER 0
WORKDIR /build

COPY pyproject.toml README.md ./
COPY src/ src/

RUN mkdir /venv && chown -R 1001:0 /build /venv
USER 1001

RUN python3.12 -m venv /venv && \
    /venv/bin/pip install . --no-deps --no-cache-dir

# Stage 2: runtime image with Git and standard toolchain
FROM registry.access.redhat.com/ubi10/ubi-minimal@sha256:3f3c6dda26caa5b2200fba25721c7a970b1acd4677bbc9865bf25dce62da918a

RUN microdnf install -y git tar gzip && microdnf clean all

COPY --from=builder /venv /venv

ENV PATH="/venv/bin:$PATH" \
    HOME="/tekton/home"

USER 1001

ENTRYPOINT ["/venv/bin/taisce-cuan"]
