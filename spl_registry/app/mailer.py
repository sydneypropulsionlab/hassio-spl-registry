"""Email. Every message is logged in the outbox; it is only sent if SMTP is configured,
so the app can be tested without a mail server (assessors see the message in Email log)."""
import smtplib
import ssl
from email.message import EmailMessage

from . import db


def send(settings, to_addr, subject, body, cc=(), attachments=()):
    """attachments: (filename, bytes, 'type/subtype') tuples."""
    cc = [c for c in cc if c and c.lower() != to_addr.lower()]
    status, error = 'not_sent', 'SMTP is not configured'
    if settings.smtp_host and settings.smtp_from:
        msg = EmailMessage()
        msg['From'] = settings.smtp_from
        msg['To'] = to_addr
        if cc:
            msg['Cc'] = ', '.join(cc)
        msg['Subject'] = subject
        msg.set_content(body)
        for filename, data, mime in attachments:
            maintype, subtype = mime.split('/', 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        try:
            if settings.smtp_port == 465:
                server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20,
                                          context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
                if settings.smtp_starttls:
                    server.starttls(context=ssl.create_default_context())
            with server:
                if settings.smtp_username:
                    server.login(settings.smtp_username, settings.smtp_password)
                server.send_message(msg)
            status, error = 'sent', None
        except Exception as e:  # recorded in the outbox for assessors to see
            status, error = 'failed', str(e)[:500]
    db.run('INSERT INTO outbox (created_at, to_addr, cc, subject, body, attachments, status, error) '
           'VALUES (?,?,?,?,?,?,?,?)', db.now(), to_addr, ', '.join(cc) or None, subject, body,
           ', '.join(a[0] for a in attachments) or None, status, error)
    return status
