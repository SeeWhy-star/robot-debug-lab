#!/bin/bash
# Install a pinned upstream release into the ignored project runtime directory.
set -euo pipefail
RDL_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != x86_64 ]; then
    echo 'This installer supports Linux x86_64 only; use the official installer for other platforms.'
    exit 1
fi
RDL_RELEASE=v0.6.9
RDL_ASSET=openfang-x86_64-unknown-linux-gnu.tar.gz
RDL_DOWNLOAD="$(mktemp -d)"
cd "$RDL_DOWNLOAD"
curl -fL --retry 2 "https://github.com/RightNow-AI/openfang/releases/download/$RDL_RELEASE/$RDL_ASSET" -o "$RDL_ASSET"
curl -fL --retry 2 "https://github.com/RightNow-AI/openfang/releases/download/$RDL_RELEASE/$RDL_ASSET.sha256" -o "$RDL_ASSET.sha256"
sha256sum -c "$RDL_ASSET.sha256"
tar -xzf "$RDL_ASSET" openfang
mkdir -p "$RDL_PROJECT_ROOT/.runtime/openfang-bin"
install -m 755 openfang "$RDL_PROJECT_ROOT/.runtime/openfang-bin/openfang"
"$RDL_PROJECT_ROOT/.runtime/openfang-bin/openfang" --version
echo "Download and checksum retained in $RDL_DOWNLOAD"
