"""163 邮箱（IMAP，客户端授权码）。"""
from control_center.credential import credential
import _imap

NAME = "163 邮箱"


def mails(since_ts, unread_only, sender, subject, newest):
    user = credential("STB_163_USER").strip()
    return (user, *_imap.mails("imap.163.com", user, credential("STB_163_PASSWORD").strip(), since_ts, unread_only, sender, subject, newest))
