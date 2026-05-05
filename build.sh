#!/bin/bash
# Install Deno
curl -fsSL https://deno.land/install.sh | sh

# Add Deno to PATH
export DENO_INSTALL="/opt/render/.deno"
export PATH="$DENO_INSTALL/bin:$PATH"

# Verify
deno --version

# Install Python dependencies
pip install -r requirements.txt
