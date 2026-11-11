# Pinned source for the release-time Cosign CLI. Copy only the binary into the
# application runtime below; the task-runner image is not the Taisce runtime.
FROM quay.io/konflux-ci/task-runner@sha256:4b01fbf98fa7155f5c21443c285f88853864ae7cc66981cf6b543fc6ba16b81b AS cosign

# Pinned source for oras + the Konflux registry-auth helper scripts used by
# package_git_source_image.py (same digest archive-sources/python-git-ingest
# already pin elsewhere). select-oci-auth depends on get-reference-base and yq;
# copy all three plus oras itself -- none of the task-runner image's other
# tooling is needed here.
FROM quay.io/konflux-ci/oras@sha256:1beeecce012c99794568f74265c065839f9703d28306a8430b667f639343a98b AS oras-tools

# Stage 1: build the wheel
FROM registry.access.redhat.com/ubi10/python-312-minimal@sha256:3bec639a17db34956fed4fa6f161d2d8cce2fa3f058f8a54134598925418c4cd as builder

USER 0
WORKDIR /build

COPY pyproject.toml README.md ./
COPY src/ src/

RUN mkdir /venv && chown -R 1001:0 /build /venv
USER 1001

RUN python3.12 -m venv /venv && \
    /venv/bin/pip install . --no-cache-dir

# Stage 2: runtime image with Git and standard toolchain
FROM registry.access.redhat.com/ubi10/python-312-minimal@sha256:3bec639a17db34956fed4fa6f161d2d8cce2fa3f058f8a54134598925418c4cd

ARG GIT_COMMIT_SHA=""
ENV GIT_COMMIT_SHA=${GIT_COMMIT_SHA}

USER 0
RUN microdnf install -y git tar gzip && microdnf clean all

COPY --from=builder /venv /venv
COPY --from=cosign /usr/local/bin/cosign /usr/local/bin/cosign
COPY --from=oras-tools /usr/bin/oras /usr/bin/oras
COPY --from=oras-tools /usr/bin/yq /usr/bin/yq
COPY --from=oras-tools /usr/local/bin/retry /usr/local/bin/retry
COPY --from=oras-tools /usr/local/bin/get-reference-base /usr/local/bin/get-reference-base
COPY --from=oras-tools /usr/local/bin/select-oci-auth /usr/local/bin/select-oci-auth

ENV PATH="/venv/bin:$PATH" \
    HOME="/tekton/home"

USER 1001

ENTRYPOINT ["/venv/bin/taisce-cuan"]
