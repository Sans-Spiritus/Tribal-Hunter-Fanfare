import os, json, sqlite3
LEVELS = [("LVMAX",1000),("LV3",100),("LV2",10),("LV1",0)]
USER_COUNTS_DIR = os.getenv("USER_COUNTS_DIR", "User Message Counts")

def _guild_dir(gid): return os.path.join(USER_COUNTS_DIR, f"guild_{gid}")
def _user_file_path(gid, uid): return os.path.join(_guild_dir(gid), f"{uid}.txt")

def _get_adjusted_count(gid, uid):
    try:
        path = _user_file_path(gid, uid)
        if not os.path.exists(path): return 0
        with open(path,"r",encoding="utf-8") as f: data=json.load(f)
        return max(0,int(data.get("adjusted_message_count",0)))
    except: return 0

def _get_live_count(db_path,gid,uid):
    con=sqlite3.connect(db_path); cur=con.cursor()
    cur.execute("SELECT count FROM message_counts WHERE guild_id=? AND user_id=?",(gid,uid))
    row=cur.fetchone(); con.close()
    return int(row[0]) if row else 0

def _get_total_and_level(db_path,gid,uid):
    total=_get_live_count(db_path,gid,uid)+_get_adjusted_count(gid,uid)
    for name,thr in LEVELS:
        if total>=thr: return total,name
    return total,"LV1"

def _meets_level(db_path,gid,uid,thr):
    total,lvl=_get_total_and_level(db_path,gid,uid)
    return (total>=thr),total,lvl