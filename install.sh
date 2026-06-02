#!/usr/bin/env bash

set -e

# Configuration
REPO_URL="https://github.com/cognato-ai/mach.git"
INSTALL_DIR="$HOME/.mach"
BIN_DIR="$HOME/.local/bin"
EXE_NAME="mach"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}Installing Mach Execution Ledger...${NC}"

# 1. Check dependencies
if ! command -v git &> /dev/null; then
    echo -e "${RED}Error: git is required but not installed.${NC}"
    exit 1
fi

if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: python3 is required but not installed.${NC}"
    exit 1
fi

# 2. Clone or update repository
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Updating existing installation..."
    cd "$INSTALL_DIR"
    git pull origin master > /dev/null 2>&1
else
    echo "Downloading Mach..."
    mkdir -p "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    git init > /dev/null 2>&1
    git remote add origin "$REPO_URL" > /dev/null 2>&1
    git fetch origin master > /dev/null 2>&1
    git reset --hard origin/master > /dev/null 2>&1
fi

# 3. Setup Virtual Environment & Install
echo "Setting up isolated Python environment..."
python3 -m venv "$INSTALL_DIR/venv" > /dev/null 2>&1
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip > /dev/null 2>&1
"$INSTALL_DIR/venv/bin/pip" install "$INSTALL_DIR" > /dev/null 2>&1

# 4. Create executable wrapper
mkdir -p "$BIN_DIR"

cat << EOF > "$BIN_DIR/$EXE_NAME"
#!/usr/bin/env bash
exec "$INSTALL_DIR/venv/bin/mach" "\$@"
EOF

chmod +x "$BIN_DIR/$EXE_NAME"

# 6. Verify PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo -e "\n${RED}Warning: $BIN_DIR is not in your PATH.${NC}"
    echo -e "To use the 'mach' command, add this line to your ~/.bashrc, ~/.zshrc, or ~/.profile:"
    echo -e "\n    ${GREEN}export PATH=\"\$HOME/.local/bin:\$PATH\"${NC}\n"
    echo "After adding it, restart your terminal or run: source ~/.zshrc"
else
    echo -e "\n${GREEN}Success! Mach was installed successfully.${NC}"
    echo -e "You can now run '${BLUE}mach init${NC}' in any repository."
fi
