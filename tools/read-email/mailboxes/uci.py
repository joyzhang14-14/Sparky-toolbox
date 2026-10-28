"""UCI Gmail（Gmail API，OAuth 凭证见 _gmail.py）。"""
from control_center.credential import credential
import _gmail

NAME = "UCI Gmail"


def mails(since_ts, unread_only, sender, subject, newest):
    account = credential("STB_GMAIL_ACCOUNT").strip()
    return (account, *_gmail.mails(account, since_ts, unread_only, sender, subject, newest))
