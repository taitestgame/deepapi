#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek API Server - OpenAI Compatible with Tool Calling"""

import sys
import os

# Force UTF-8 encoding for stdout/stderr on Windows
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

# Load env before anything else
from config import load_env
load_env()

from flask import Flask
from routes import (
    health, list_models, chat_completions, log_req, log_resp, after_request
)
from token_manager import prelogin_all_accounts

# Create Flask app
app = Flask(__name__)

# Register hooks
app.before_request(log_req)
app.after_request(log_resp)
app.after_request(after_request)  # CORS

# Register routes
app.route('/health', methods=['GET'])(health)
app.route('/v1/models', methods=['GET'])(list_models)
app.route('/v1/chat/completions', methods=['POST', 'OPTIONS'])(chat_completions)

# Background token refresh
prelogin_all_accounts()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    host = os.environ.get('HOST', '0.0.0.0')
    app.run(host=host, port=port, debug=False)
