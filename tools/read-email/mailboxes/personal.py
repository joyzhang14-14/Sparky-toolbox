"""私人 Gmail（IMAP，应用专用密码）。"""
from control_center.credential import credential
import _imap

NAME = "私人 Gmail"


def mails(since_ts, unread_only, sender, subject, newest):
    user = credential("STB_GMAIL2_USER").strip()
    return (user, *_imap.mails("imap.gmail.com", user, credential("STB_GMAIL2_APP_PASSWORD").strip(), since_ts, unread_only, sender, subject, newest))
