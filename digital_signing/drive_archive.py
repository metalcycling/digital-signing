"""Archive completed PDFs in Drive without coupling signing to network availability."""
from fastapi import HTTPException


def install_drive_archive(app, db, lock, settings, provider):
    with db() as con:
        columns = {row[1] for row in con.execute('PRAGMA table_info(submissions)')}
        for name in ('drive_file_id', 'drive_file_url', 'drive_save_error'):
            if name not in columns:
                con.execute(f'ALTER TABLE submissions ADD COLUMN {name} TEXT')

    def save(contract_id, version):
        if settings.mode != 'google':
            return {'status':'unavailable', 'url':None, 'error':None}
        with lock:
            with db() as con:
                contract = con.execute('SELECT * FROM contracts WHERE id=?', (contract_id,)).fetchone()
                row = con.execute('SELECT * FROM submissions WHERE contract_id=? AND version=?', (contract_id,version)).fetchone()
                latest = con.execute('SELECT MAX(version) FROM renders WHERE contract_id=?', (contract_id,)).fetchone()[0]
                if not contract or not row or not row['final_pdf'] or contract['status'] != 'completed' or latest != version:
                    raise HTTPException(409, 'The latest agreement must be fully executed before saving its Drive PDF.')
                if row['drive_file_url']:
                    return {'status':'saved', 'url':row['drive_file_url'], 'error':None}
                contract = dict(contract)
                content, file_id = bytes(row['final_pdf']), row['drive_file_id']
            try:
                drive = provider()
                if not file_id:
                    file_id = drive.reserve_pdf_id()
                    with db() as con:
                        con.execute('UPDATE submissions SET drive_file_id=? WHERE contract_id=? AND version=?', (file_id,contract_id,version))
                saved = drive.save_final_pdf(contract,version,file_id,content)
                url = saved.get('webViewLink')
                if not url:
                    raise ValueError('Drive did not return a PDF link.')
                with db() as con:
                    con.execute('UPDATE submissions SET drive_file_url=?,drive_save_error=NULL WHERE contract_id=? AND version=?', (url,contract_id,version))
                return {'status':'saved', 'url':url, 'error':None}
            except Exception:
                error = 'The signed PDF is saved locally, but could not be saved to Google Drive. Check Google access and use Copy link in the workspace to retry.'
                with db() as con:
                    con.execute('UPDATE submissions SET drive_save_error=? WHERE contract_id=? AND version=?', (error,contract_id,version))
                return {'status':'failed', 'url':None, 'error':error}

    app.state.save_final_pdf_to_drive = save

    @app.post('/api/contracts/{contract_id}/submissions/{version}/drive')
    def archive(contract_id: str, version: int):
        result = save(contract_id,version)
        if result['status'] != 'saved':
            raise HTTPException(502 if result['status']=='failed' else 409, result['error'] or 'Google Drive links are unavailable in demo mode.')
        return result
