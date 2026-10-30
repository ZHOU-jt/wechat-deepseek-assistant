"""Local control panel. Opening this UI never accesses WeChat or DeepSeek."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from core import DEFAULT_PROMPT, normalize_groups
from secure_storage import load_api_key, save_api_key

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "settings.json"
PROMPT = HERE / "prompt.txt"
CACHE = HERE / "cache"

class App:
    def __init__(self, root):
        self.root, self.process, self.mode = root, None, None
        self.events = queue.Queue()
        self.checked_groups = None
        root.title("微信聊天助手设置")
        root.geometry("760x740")
        root.minsize(700, 700)
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style()
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=6)
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="微信聊天助手", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        ttk.Label(frame, text="微信保持登录主界面即可，无需打开独立聊天窗口。", wraplength=700).pack(anchor="w", pady=(6,12))
        settings = {"model":"deepseek-flash", "prefix":"@助手", "groups":[]}
        try:
            settings.update(json.loads(CONFIG.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        key_error = None
        try:
            saved_key = load_api_key(CACHE)
        except RuntimeError as error:
            saved_key, key_error = '', str(error)
        self.key = tk.StringVar(value=saved_key)
        self.model = tk.StringVar(value=settings["model"])
        self.prefix = tk.StringVar(value=settings["prefix"])
        form = ttk.Frame(frame)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        for row, (title, variable, hide) in enumerate([
            ("DeepSeek API Key",self.key,True), ("模型",self.model,False), ("触发词",self.prefix,False)]):
            ttk.Label(form,text=title).grid(row=row,column=0,sticky="w",pady=4,padx=(0,12))
            ttk.Entry(form,textvariable=variable,show="●" if hide else "").grid(row=row,column=1,sticky="ew",pady=4)
        ttk.Label(form,text="目标群名\n每行一个完整名称").grid(row=3,column=0,sticky="nw",pady=4,padx=(0,12))
        self.groups = tk.Text(form,height=3,wrap="none",font=("Microsoft YaHei UI",10))
        self.groups.grid(row=3,column=1,sticky="ew",pady=4)
        self.groups.insert("1.0", "\n".join(normalize_groups(settings["groups"])))
        self.all_friends = tk.BooleanVar(value=settings.get('all_friends', False))
        friend_toggle = ttk.Checkbutton(form, text='所有普通好友私聊自动回复（无需触发词，仅文字）', variable=self.all_friends)
        friend_toggle.grid(row=4, column=0, columnspan=2, sticky='w', pady=4)
        ttk.Label(frame,text="Key 使用当前 Windows 账号加密保存，下次自动填入；只发送给 api.deepseek.com。",wraplength=700).pack(anchor="w",pady=6)
        ttk.Label(frame,text="提示词（保存后写入旁边的 prompt.txt；修改后需停止并重新启动）").pack(anchor="w",pady=(5,4))
        self.prompt = tk.Text(frame,height=5,wrap="word",font=("Microsoft YaHei UI",10))
        self.prompt.pack(fill="x")
        self.prompt.insert("1.0", PROMPT.read_text(encoding="utf-8") if PROMPT.exists() else DEFAULT_PROMPT)
        row1=ttk.Frame(frame); row1.pack(fill="x",pady=(12,4))
        self.controls=[self.groups, friend_toggle]
        for label, command in [("保存设置",self.save),("测试 API",lambda:self.run("api")),("检查微信读取",lambda:self.run("check"))]:
            button=ttk.Button(row1,text=label,command=command);button.pack(side="left",padx=(0,8));self.controls.append(button)
        row2=ttk.Frame(frame);row2.pack(fill="x",pady=4)
        for label, command in [("启动自动回复",self.start)]:
            button=ttk.Button(row2,text=label,command=command);button.pack(side="left",padx=(0,8));self.controls.append(button)
        ttk.Button(row2,text="停止",command=self.stop).pack(side="left")
        ttk.Label(frame,text="群聊继续使用触发词；私聊与各群分别缓存，重启恢复。\n发送时自动切换微信主窗口；请勿同时手动切换会话。",wraplength=700).pack(anchor="w",pady=8)
        self.log=tk.Text(frame,height=7,state="disabled",wrap="word",font=("Microsoft YaHei UI",10))
        self.log.pack(fill="both",expand=True)
        self.write("待配置。尚未调用 API 或启动群消息监听。")
        if key_error:
            self.write("API Key 未恢复：" + key_error)
        root.after(150,self.poll)

    def write(self,text):
        self.log.configure(state="normal");self.log.insert("end",text+"\n");self.log.see("end");self.log.configure(state="disabled")

    def values(self):
        if not self.model.get().strip() or not self.prefix.get().strip():
            raise ValueError("模型和触发词不能为空。")
        return {"key":self.key.get().strip(),"model":self.model.get().strip(),"prefix":self.prefix.get().strip(),"prompt":self.prompt.get("1.0","end").strip(),"groups":normalize_groups(self.groups.get("1.0","end")), 'all_friends':self.all_friends.get(), 'main_window':True}

    def save(self):
        try:
            data=self.values()
            save_api_key(CACHE, data['key'])
            CONFIG.write_text(json.dumps({"model":data["model"],"prefix":data["prefix"],"groups":data["groups"], 'all_friends':data['all_friends']},ensure_ascii=False,indent=2),encoding="utf-8")
            PROMPT.write_text(data["prompt"],encoding="utf-8")
            self.write("设置和提示词已保存；API Key 已加密保存。" if data['key'] else "设置和提示词已保存；保存的 API Key 已清除。")
        except Exception as error:
            messagebox.showerror("保存失败",str(error))

    def start(self):
        try:
            data = self.values()
            groups = tuple(data['groups'])
            signature = (groups, data['all_friends'])
        except ValueError as error:
            messagebox.showerror("未启动", str(error))
            return
        if (not groups and not data['all_friends']) or self.checked_groups != signature:
            messagebox.showinfo("先检查读取","请先选择群聊或好友私聊，再点击“检查微信读取”；修改监听范围后需要重新检查。")
            return
        scope = '\n所有普通好友的新文字私聊：直接回复，无需触发词。' if data['all_friends'] else '\n好友私聊回复：关闭。'
        if messagebox.askyesno("启动自动回复","目标群（需触发词）：\n" + ("\n".join(groups) or '无') + scope + "\n\n回复时会将本会话的提问、昵称、最近最多40条文字和当前成员最近5轮对话发给 DeepSeek。不同群和私聊互不混用。\n\n缓存和 Key 加密保存，重启恢复；不补答启动前的旧消息。确认聊天参与者知情后，点击“是”启动。"):
            self.run("listen")

    def run(self,mode):
        if self.process and self.process.poll() is None:
            return
        try:
            data=self.values()
            if mode in ("api","listen") and not data["key"]:
                raise ValueError("请先在窗口内填写 DeepSeek API Key。")
            if mode in ("check", "listen") and not data["groups"] and not data['all_friends']:
                raise ValueError("请填写目标群名，或开启所有好友私聊回复。")
            if mode in ('api', 'listen'):
                save_api_key(CACHE, data['key'])
            if mode=="check":self.checked_groups=None
            env=os.environ.copy();env["PYTHONIOENCODING"]="utf-8";env["PYTHONUTF8"]="1"
            process=subprocess.Popen([sys.executable,str(HERE/"worker.py"),mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",env=env,cwd=HERE,creationflags=subprocess.CREATE_NO_WINDOW)
            self.process,self.mode=process,mode
            process.stdin.write(json.dumps(data,ensure_ascii=False)+"\n");process.stdin.flush();process.stdin.close()
            for button in self.controls:button.configure(state="disabled")
            self.write({"api":"正在测试 DeepSeek（仅发送固定测试文字）…","check":"正在检查微信主窗口和消息库，不发消息…","listen":"正在启动…"}[mode])
            def read():
                for line in process.stdout:
                    try:
                        item=json.loads(line)
                        if "status" in item:self.events.put((process,item["status"]))
                    except ValueError:pass
                code=process.wait();self.events.put((process,("done",code,mode,(tuple(data["groups"]),data['all_friends']))))
            threading.Thread(target=read,daemon=True).start()
        except Exception as error:
            messagebox.showerror("未启动",str(error))

    def poll(self):
        while not self.events.empty():
            process,event=self.events.get()
            if process is not self.process:continue
            if isinstance(event,tuple):
                _,code,mode,groups=event
                if mode=="check" and code==0:self.checked_groups=groups
                for button in self.controls:button.configure(state="normal")
                self.process=None
            else:self.write(event)
        self.root.after(150,self.poll)

    def stop(self):
        process=self.process
        if process and process.poll() is None:
            process.terminate();process.wait(timeout=5)
            self.write("已停止。未发送的回复已丢弃；已发出的消息仍保留在群内。")
        self.process=None
        for button in self.controls:button.configure(state="normal")

    def close(self):
        self.stop();self.root.destroy()

if __name__=="__main__":
    root=tk.Tk();App(root);root.mainloop()
