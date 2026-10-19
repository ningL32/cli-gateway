"""cli-gateway serve：分子宇宙实时视图（仅绑定本机回环）。

星系态（/）：全部化合物按元素色卡漂浮在螺旋球面，运行中任务脉动；
分子态（#/task/<id>）：单任务球棍模型 + 对话树下钻。
同一 SPA 两态，5s 轮询 /api/* 刷新——正在跑的调用实时长出原子。

安全边界：只绑 127.0.0.1、零外部请求、只读扫描日志目录、无认证——
本质是"本机日志的浏览器视图"，与 `task --html` 同级，仅多了实时性。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import DEFAULT_LOG_DIR
from .events import build_graph, galaxy

_DEFAULT_PORT = 7817


def _log_root(log_dir: str | None) -> str:
    return str(Path(log_dir)) if log_dir else str(DEFAULT_LOG_DIR)


def make_server(port: int = _DEFAULT_PORT, log_dir: str | None = None) -> ThreadingHTTPServer:
    """构建 serve 实例（bind 127.0.0.1）。返回 ThreadingHTTPServer，测试可直接启停。"""
    root = _log_root(log_dir)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: str, ctype: str) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - http.server 约定
            u = urlparse(self.path)
            if u.path == "/" or u.path.startswith("/task/"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif u.path == "/api/galaxy":
                self._send(200, json.dumps(galaxy(root), ensure_ascii=False), "application/json; charset=utf-8")
            elif u.path == "/api/graph":
                qs = parse_qs(u.query)
                task_id = (qs.get("task") or [""])[0]
                if not task_id:
                    self._send(400, json.dumps({"error": "missing task"}), "application/json")
                    return
                self._send(200, json.dumps(build_graph(task_id, root), ensure_ascii=False), "application/json; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain")

        def log_message(self, *args: object) -> None:  # 静默访问日志
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(port: int = _DEFAULT_PORT, log_dir: str | None = None) -> None:
    """启动分子宇宙实时视图（阻塞；Ctrl+C 停止）。"""
    httpd = make_server(port, log_dir)
    print(f"cli-gateway 分子宇宙 → http://127.0.0.1:{httpd.server_address[1]}  （Ctrl+C 停止）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>cli-gateway · 分子宇宙</title>
<style>
  html,body{margin:0;height:100%;background:#04060c;overflow:hidden;font-family:"Segoe UI",system-ui,sans-serif;color:#cbd5e1}
  #sky{position:fixed;inset:0}
  #scene{position:fixed;inset:0;z-index:1}
  #hud{position:fixed;top:14px;left:18px;z-index:5;pointer-events:none}
  #hud h1{margin:0;font-size:18px;color:#e2e8f0;letter-spacing:.5px}
  #hud .meta{margin-top:4px;font-size:11px;color:#64748b}
  #back{position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:7;display:none;cursor:pointer;color:#38bdf8;font-size:13px}
  #tasklist{position:fixed;top:70px;left:18px;z-index:5;max-width:340px;max-height:70vh;overflow-y:auto}
  .tcard{cursor:pointer;margin:8px 0;padding:8px 12px;border:1px solid #1e293b;border-radius:10px;
         background:rgba(15,23,42,.55);font-size:12px}
  .tcard:hover{border-color:#38bdf8}
  .run{display:inline-block;width:8px;height:8px;border-radius:50%;background:#34d399;margin-right:6px;
       animation:pulse 1.2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
  #legend{position:fixed;bottom:14px;left:18px;z-index:5;font-size:11px;color:#64748b}
  #legend b{display:inline-block;width:8px;height:8px;border-radius:50%;margin:0 4px 0 10px}
  #hint{position:fixed;bottom:14px;right:18px;z-index:5;font-size:11px;color:#475569}
  #panel{position:fixed;top:0;right:-400px;width:380px;height:100%;background:rgba(8,12,22,.93);
         border-left:1px solid #1e293b;transition:right .25s;z-index:6;padding:18px;box-sizing:border-box;overflow-y:auto}
  #panel.open{right:0}
  #panel h3{margin:0 0 4px;color:#e2e8f0;font-size:15px}
  #panel .sub{font-size:11px;color:#64748b;margin-bottom:10px}
  .ev{margin:7px 0;font-size:12px;line-height:1.5;border-left:2px solid #334155;padding-left:8px}
  .ev b{font-size:10px;letter-spacing:.5px;padding:0 5px;border-radius:3px;margin-right:6px}
</style>
</head>
<body>
<canvas id="sky"></canvas>
<canvas id="scene"></canvas>
<div id="back">← 返回星系</div>
<div id="hud"><h1 id="title">分子宇宙</h1><div class="meta" id="meta"></div></div>
<div id="tasklist"></div>
<div id="legend"></div>
<div id="hint">拖拽旋转 · 滚轮缩放 · 点击下钻 · 5s 实时刷新</div>
<div id="panel"><span id="close" style="cursor:pointer;color:#64748b">✕</span>
<h3 id="p-title"></h3><div class="sub" id="p-sub"></div><div id="p-evs"></div></div>
<script>
const KIND_COLOR={question:"#94a3b8",text:"#38bdf8",tool_call:"#f59e0b",tool_result:"#a3e635",
  reasoning:"#c084fc",result:"#34d399",error:"#f87171",answer:"#e2e8f0",exit:"#64748b",note:"#475569",
  system:"#334155"};
const ORBIT=["#818cf8","#38bdf8","#34d399","#f59e0b","#f472b6","#e879f9","#fb7185","#a3e635"];
const sky=document.getElementById("sky"),sctx=sky.getContext("2d");
function paintSky(){sky.width=innerWidth;sky.height=innerHeight;sctx.fillStyle="#04060c";
  sctx.fillRect(0,0,sky.width,sky.height);
  for(let i=0;i<240;i++){sctx.fillStyle="rgba(148,163,184,"+(Math.random()*.5+.1)+")";
    sctx.beginPath();sctx.arc(Math.random()*sky.width,Math.random()*sky.height,Math.random()*1.1+.2,0,7);sctx.fill();}}
paintSky();
function spiral(n){const p=[],ga=Math.PI*(3-Math.sqrt(5));
  for(let i=0;i<n;i++){const y=1-2*(i+.5)/Math.max(n,1),r=Math.sqrt(Math.max(0,1-y*y)),th=ga*i;
    p.push([Math.cos(th)*r,y,Math.sin(th)*r]);}return p;}

const cv=document.getElementById("scene"),ctx=cv.getContext("2d");
let mode="galaxy",nodes=[],links=[],view=null;
let rotY=.6,tiltX=-.25,zoom=1,spin=.0025,dragging=false,lx=0,ly=0;
const R=()=>Math.min(innerWidth,innerHeight)*.33;
function resize(){cv.width=innerWidth;cv.height=innerHeight}resize();
addEventListener("resize",()=>{paintSky();resize()});

function project(p){const[x,y,z]=p,cy=Math.cos(rotY),sy=Math.sin(rotY);
  let x1=x*cy+z*sy,z1=-x*sy+z*cy;const cx=Math.cos(tiltX),sx=Math.sin(tiltX);
  let y1=y*cx-z1*sx,z2=y*sx+z1*cx;const f=1/(1+z2/3.2);
  return{x:innerWidth/2+x1*R()*zoom*f,y:innerHeight/2+y1*R()*zoom*f,z:z2,f};}
function ring(x,y,r,c,d){ctx.strokeStyle=c;ctx.lineWidth=1.4;if(d)ctx.setLineDash([4,4]);
  ctx.beginPath();ctx.arc(x,y,r,0,7);ctx.stroke();ctx.setLineDash([]);}

function draw(){ctx.clearRect(0,0,cv.width,cv.height);
  if(!dragging)rotY+=spin;
  const proj=nodes.map((n,i)=>({...project(n.base),n,i}));
  if(mode==="task")for(const l of links){const a=proj[l.source],b=proj[l.target];if(!a||!b)continue;
    ctx.strokeStyle="rgba(100,116,139,"+(0.12+Math.min(0.45,(l.weight||200)/8000))+")";
    ctx.lineWidth=0.8+Math.min(3.2,(l.weight||200)/2500);
    ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();}
  proj.sort((a,b)=>b.z-a.z).forEach(p=>{const n=p.n;
    const r=(mode==="task"?8+Math.min(26,(n.duration_ms||1000)/4000):13+Math.min(30,(n.n||1)*7))*p.f;
    p.sr=r;
    const g=ctx.createRadialGradient(p.x,p.y,0,p.x,p.y,r*2.6);
    g.addColorStop(0,n.color+"cc");g.addColorStop(.45,n.color+"44");g.addColorStop(1,n.color+"00");
    ctx.fillStyle=g;ctx.beginPath();ctx.arc(p.x,p.y,r*2.6,0,7);ctx.fill();
    ctx.fillStyle=n.color;ctx.beginPath();ctx.arc(p.x,p.y,r*.62,0,7);ctx.fill();
    if(n.running){const pu=.5+.5*Math.sin(Date.now()/180);ring(p.x,p.y,r*1.3,"rgba(52,211,153,"+pu+")",false);}
    else if(n.state==="timed_out")ring(p.x,p.y,r,"#f59e0b",true);
    else if((n.exit_code||0)!==0&&n.exit_code!==null&&n.state!=="not_found")ring(p.x,p.y,r,"#f87171",false);
    else if(mode==="task"&&n.state==="completed")ring(p.x,p.y,r*1.15,"rgba(226,232,240,.35)",false);
    ctx.fillStyle="rgba(226,232,240,.85)";ctx.font=(10*p.f).toFixed(1)+"px system-ui";ctx.textAlign="center";
    ctx.fillText(mode==="task"?n.element:(n.label||n.element),p.x,p.y-r*1.5);});
  cv._proj=proj;
  requestAnimationFrame(draw);}
draw();

function setNodes(list){const pts=spiral(list.length);
  nodes=list.map((n,i)=>({...n,base:pts[i]}));}

async function loadGalaxy(){mode="galaxy";view=null;nodes=[];links=[];
  document.getElementById("back").style.display="none";
  document.getElementById("title").textContent="分子宇宙";
  const d=await (await fetch("/api/galaxy")).json();
  document.getElementById("meta").textContent=d.tasks.length+" compounds · generated "+d.generated_at;
  const tl=document.getElementById("tasklist");
  tl.innerHTML=d.tasks.map((t,i)=>'<div class="tcard" data-i="'+i+'">'+
    (t.running?'<span class="run"></span>':"")+"<b style='color:"+ORBIT[i%ORBIT.length]+"'>"+t.task+
    "</b><br><span style='color:#64748b'>"+t.formula+" · "+t.n+" atoms"+
    (t.running?" · "+t.running+" running":"")+"</span></div>").join("")
    ||'<div class="tcard">暂无化合物——用 run --observe --task-id &lt;id&gt; 创建</div>';
  tl.querySelectorAll(".tcard").forEach(el=>el.onclick=()=>{location.hash="#/task/"+encodeURIComponent(d.tasks[+el.dataset.i].task);});
  setNodes(d.tasks.map((t,i)=>({label:t.task,color:ORBIT[i%ORBIT.length],n:t.n,
    running:t.running>0,state:t.running>0?"running":"completed",exit_code:t.running>0?0:null,
    duration_ms:t.n*1000,task:t.task})));
  document.getElementById("legend").innerHTML="星系：每个发光球 = 一个化合物（大小 ∝ 原子数，绿脉 = 运行中）";}
async function loadTask(id){mode="task";view=id;links=[];
  document.getElementById("back").style.display="block";
  document.getElementById("title").textContent="task · "+id;
  document.getElementById("tasklist").innerHTML="";
  const d=await (await fetch("/api/graph?task="+encodeURIComponent(id))).json();
  if(!d.nodes){setNodes([]);return;}
  document.getElementById("meta").textContent=d.formula+" · "+d.nodes.length+" atoms · generated "+d.generated_at;
  setNodes(d.nodes);
  const els=[...new Set(d.nodes.map(n=>n.element))];
  document.getElementById("legend").innerHTML="元素色卡："+els.map(e=>{
    const c=d.nodes.find(n=>n.element===e).color;return "<b style='background:"+c+"'></b>"+e;}).join("");
  if(d.nodes.some(n=>n.running))setTimeout(()=>{if(view===id&&mode==="task")loadTask(id);},5000);}

cv.addEventListener("mousedown",e=>{dragging=true;lx=e.clientX;ly=e.clientY});
addEventListener("mouseup",()=>dragging=false);
addEventListener("mousemove",e=>{if(!dragging)return;
  rotY+=(e.clientX-lx)*.006;tiltX=Math.max(-1.2,Math.min(1.2,tiltX+(e.clientY-ly)*.004));
  lx=e.clientX;ly=e.clientY;});
cv.addEventListener("wheel",e=>{zoom=Math.max(.5,Math.min(2.6,zoom*(e.deltaY<0?1.08:.93)));e.preventDefault();},{passive:false});
cv.addEventListener("click",e=>{const proj=cv._proj||[];let best=-1,bd=1e9;
  proj.forEach((p,i)=>{const d=Math.hypot(e.clientX-p.x,e.clientY-p.y);
    if(d<Math.max(14,p.sr||12)+8&&d<bd){bd=d;best=i;}});
  if(best<0)return;const n=proj[best].n;
  if(mode==="galaxy"){location.hash="#/task/"+encodeURIComponent(n.task);return;}
  openPanel(n);});
function openPanel(n){
  document.getElementById("panel").classList.add("open");
  document.getElementById("p-title").textContent=n.element+" · "+n.id;
  document.getElementById("p-sub").textContent="exit="+n.exit_code+"  state="+n.state+"  "+n.duration_ms+"ms  events="+(n.n_events||0);
  document.getElementById("p-evs").innerHTML=(n.preview||[]).map(ev=>{
    const c=KIND_COLOR[ev.kind]||"#475569";
    return '<div class="ev" style="border-color:'+c+'66"><b style="color:'+c+';border:1px solid '+c+'55">'+ev.kind+
      '</b>'+(ev.summary||"").replace(/&/g,"&amp;").replace(/</g,"&lt;")+'</div>';}).join("")
    ||'<div class="ev">（无事件预览——sidecar 缺失或未观测）</div>';}
document.getElementById("close").onclick=()=>document.getElementById("panel").classList.remove("open");
document.getElementById("back").onclick=()=>{location.hash="";};
setInterval(()=>{if(mode==="galaxy")loadGalaxy();},5000);
addEventListener("hashchange",()=>{const m=location.hash.match(/^#\/task\/(.+)$/);
  m?loadTask(decodeURIComponent(m[1])):loadGalaxy();});
{const m=location.hash.match(/^#\/task\/(.+)$/);m?loadTask(decodeURIComponent(m[1])):loadGalaxy();}
</script>
</body>
</html>
"""
