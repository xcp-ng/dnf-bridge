FROM almalinux:10

# uv reads the project dependencies from pyproject.toml/uv.lock
COPY --from=ghcr.io/astral-sh/uv:0.10.2 /uv /usr/local/bin/uv

RUN dnf -y install python3 git \
    && dnf clean all

# install the project dependencies into a venv which still sees the system
# dnf module (include-system-site-packages)
COPY pyproject.toml uv.lock /opt/dnf-bridge/
RUN cd /opt/dnf-bridge \
    && uv export --frozen --no-dev --no-emit-project --no-hashes > requirements.txt \
    && uv venv --python /usr/bin/python3 --system-site-packages .venv \
    && uv pip install --python .venv/bin/python -r requirements.txt \
    && rm requirements.txt

# run as: docker run <image> <path to gen-dnf-proxy.py> <path to the layer>
ENTRYPOINT ["/opt/dnf-bridge/.venv/bin/python"]
