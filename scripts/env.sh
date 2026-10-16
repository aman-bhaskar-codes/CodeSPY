# Source this file from Bash or Zsh to select project-local tools.
if [ -n "${ZSH_VERSION:-}" ]; then
  CODESPY_ENV_FILE=${(%):-%x}
else
  CODESPY_ENV_FILE=${BASH_SOURCE:-$0}
fi
CODESPY_ROOT=$(CDPATH= cd -- "$(dirname -- "$CODESPY_ENV_FILE")/.." && pwd)
export CODESPY_ROOT
export PATH="$CODESPY_ROOT/.tools/bin:$CODESPY_ROOT/frontend/node_modules/.bin:$PATH"
if [ -d /opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home ]; then
  export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
  export PATH="$JAVA_HOME/bin:$PATH"
fi
export GHIDRA_INSTALL_DIR="$CODESPY_ROOT/.tools/ghidra/ghidra_12.1.4_PUBLIC"
export PATH="$GHIDRA_INSTALL_DIR/support:$PATH"
export SSL_CERT_FILE=${SSL_CERT_FILE:-/etc/ssl/cert.pem}

if command -v fnm >/dev/null 2>&1; then
  eval "$(fnm env --shell bash)"
  fnm use --silent-if-unchanged "$(cat "$CODESPY_ROOT/.nvmrc")" >/dev/null
fi
