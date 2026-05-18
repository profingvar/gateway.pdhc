"""Downloadable documentation routes."""
import os
from flask import send_from_directory, render_template
from . import web_bp
from .auth import require_login


DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'docs')


@web_bp.route('/docs')
@require_login
def docs_index():
    """List available documentation files."""
    docs = []
    abs_dir = os.path.abspath(DOCS_DIR)
    if os.path.isdir(abs_dir):
        for f in sorted(os.listdir(abs_dir)):
            if f.endswith('.md'):
                title = f.replace('_', ' ').replace('.md', '').title()
                docs.append({'filename': f, 'title': title})
    return render_template('docs_index.html', docs=docs)


@web_bp.route('/docs/download/<filename>')
@require_login
def download_doc(filename):
    """Download a documentation file."""
    abs_dir = os.path.abspath(DOCS_DIR)
    if not filename.endswith('.md'):
        return 'Not found', 404
    return send_from_directory(abs_dir, filename, as_attachment=True)
