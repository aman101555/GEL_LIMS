import{r,j as e}from"./index-C4mzkx3I.js";const f="https://files.glabint.com",I=`
  @import url('https://fonts.googleapis.com/css2?family=DM+Sans:ital,wght@0,300;0,400;0,500;0,600&family=DM+Mono:wght@400;500&display=swap');

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  .db-root {
    font-family: 'DM Sans', sans-serif;
    background: #ffffff;
    color: #1a202c;
    height: 100vh;
    display: flex;
    flex-direction: column;
    font-size: 13.5px;
    line-height: 1.5;
    overflow: hidden;
  }

  /* ─── TOPBAR ─── */
  .db-topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 20px;
    height: 50px;
    background: #ffffff;
    border-bottom: 1px solid #e8ecf2;
    flex-shrink: 0;
    z-index: 10;
  }
  .db-logo {
    font-size: 15px;
    font-weight: 600;
    color: #0f172a;
    letter-spacing: -0.2px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .db-logo-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: #3a7bd5;
    box-shadow: 0 0 8px #3a7bd566;
  }
  .db-status {
    display: flex; align-items: center; gap: 5px;
    font-size: 11.5px; font-weight: 500;
    padding: 3px 10px; border-radius: 20px;
    background: #f4f6f9; border: 1px solid #dde2ec;
    color: #5a6a80;
  }
  .db-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  .db-dot.on  { background: #22d3a0; box-shadow: 0 0 6px #22d3a055; }
  .db-dot.off { background: #f87171; box-shadow: 0 0 6px #f8717155; }
  .db-dot.chk { background: #fbbf24; }
  .db-nav { display: flex; gap: 6px; }
  .db-btn {
    display: flex; align-items: center; gap: 5px;
    padding: 5px 13px; border-radius: 7px;
    border: 1px solid #dde2ec; background: #f4f6f9;
    color: #4a5568; font-size: 12.5px;
    font-family: 'DM Sans', sans-serif; font-weight: 500;
    cursor: pointer; transition: all 0.13s;
  }
  .db-btn:hover { background: #e8edf5; color: #1a202c; border-color: #c8d4e0; }
  .db-btn svg { width: 13px; height: 13px; flex-shrink: 0; }

  /* ─── BREADCRUMB ─── */
  .db-crumb {
    display: flex; align-items: center;
    padding: 0 20px; height: 32px;
    background: #f8f9fc;
    border-bottom: 1px solid #e8ecf2;
    flex-shrink: 0; overflow-x: auto;
    scrollbar-width: none;
  }
  .db-crumb::-webkit-scrollbar { display: none; }
  .db-crumb-seg {
    display: flex; align-items: center;
    font-size: 11px; font-family: 'DM Mono', monospace;
    color: #b0bac8; white-space: nowrap;
    cursor: pointer; transition: color 0.12s;
  }
  .db-crumb-seg:hover .db-crumb-label { color: #4a6890; }
  .db-crumb-seg.last .db-crumb-label { color: #3a6090; }
  .db-crumb-sep { margin: 0 5px; color: #c8d4e0; font-size: 10px; user-select: none; }

  /* ─── COLUMNS AREA ─── */
  .db-columns-wrap {
    flex: 1; display: flex; overflow: hidden; position: relative;
  }
  .db-columns-scroll {
    flex-shrink: 0;
    display: flex; align-items: flex-start;
    overflow-x: auto; overflow-y: hidden;
    padding: 20px 24px 20px 24px;
    scrollbar-width: none;
  }
  .db-columns-scroll.no-file {
    flex: 1;
  }
  .db-columns-scroll::-webkit-scrollbar { display: none; }

  /* ─── COLUMN CARD ─── */
  .db-col {
    flex-shrink: 0;
    width: 226px;
    height: calc(100vh - 50px - 32px - 40px);
    background: #f9fafb;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    position: relative;
    animation: slideIn 0.18s ease-out both;
    margin-right: -20px;
  }
  .db-col:last-of-type {
    margin-right: 0;
    border-color: #cdd7e8;
  }

  .db-col { box-shadow: 4px 0 20px #0000000a, 0 4px 20px #00000008; }
  .db-col:last-of-type { box-shadow: 0 8px 30px #00000012; }

  .db-col:nth-child(1) { background: #fafbfc; }
  .db-col:nth-child(2) { background: #f8f9fb; }
  .db-col:nth-child(3) { background: #f5f7fa; }
  .db-col:nth-child(4) { background: #f2f5f9; }
  .db-col:nth-child(n+5) { background: #f0f3f8; }

  @keyframes slideIn {
    from { opacity: 0; transform: translateX(28px) scale(0.97); }
    to   { opacity: 1; transform: translateX(0)    scale(1);    }
  }

  .db-col-header {
    padding: 11px 13px 9px;
    border-bottom: 1px solid #e2e8f0;
    flex-shrink: 0;
  }
  .db-col-title {
    font-size: 10.5px; font-weight: 600;
    letter-spacing: 0.06em; text-transform: uppercase;
    color: #a0aec0;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .db-col-title.root-lbl { color: #3b82f6; }

  .db-col-list {
    flex: 1; overflow-y: auto; padding: 5px 5px 10px;
    scrollbar-width: thin; scrollbar-color: #d1dbe8 transparent;
  }
  .db-col-list::-webkit-scrollbar { width: 3px; }
  .db-col-list::-webkit-scrollbar-thumb { background: #d1dbe8; border-radius: 3px; }

  .db-item {
    display: flex; align-items: center; gap: 7px;
    padding: 6px 8px; border-radius: 7px;
    cursor: pointer; transition: background 0.1s;
    margin-bottom: 1px;
  }
  .db-item:hover { background: #eef2f8; }
  .db-item.active-folder { background: #dbeafe; }
  .db-item.active-file   { background: #eff6ff; }

  .db-iicon { width: 14px; height: 14px; flex-shrink: 0; }
  .db-iname {
    font-size: 13px; font-weight: 400; color: #4a6080;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1;
  }
  .db-item:hover .db-iname { color: #1e3a5f; }
  .db-item.active-folder .db-iname,
  .db-item.active-file   .db-iname { color: #1e40af; }

  .db-chevron { width: 10px; height: 10px; color: #a0b4cc; flex-shrink: 0; }
  .db-item.active-folder .db-chevron { color: #2563eb; }

  /* ─── FILE PANEL (fills remaining space) ─── */
  .db-file-panel {
    flex: 1;
    border-left: 1px solid #e2e8f0;
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    animation: fadeIn 0.18s ease-out both;
    scrollbar-width: thin; scrollbar-color: #d1dbe8 transparent;
    background: #ffffff;
  }
  .db-file-panel::-webkit-scrollbar { width: 3px; }
  .db-file-panel::-webkit-scrollbar-thumb { background: #d1dbe8; border-radius: 3px; }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateX(10px); }
    to   { opacity: 1; transform: translateX(0); }
  }

  .db-panel-header {
    padding: 28px 32px 24px;
    border-bottom: 1px solid #e8ecf2;
    flex-shrink: 0;
  }
  .db-panel-ficon {
    width: 44px; height: 44px;
    background: #eff6ff; border: 1px solid #bfdbfe;
    border-radius: 11px;
    display: flex; align-items: center; justify-content: center;
    margin-bottom: 14px;
  }
  .db-panel-name {
    font-size: 15px; font-weight: 600; color: #0f172a;
    margin-bottom: 5px; word-break: break-word;
  }
  .db-panel-path {
    font-family: 'DM Mono', monospace; font-size: 10.5px;
    color: #94a3b8;
    word-break: break-all; line-height: 1.7;
  }

  .db-panel-actions {
    padding: 28px 32px;
    display: flex;
    gap: 20px;
    flex-wrap: wrap;
    align-items: flex-start;
  }

  .db-panel-action-block {
    flex: 1;
    min-width: 180px;
  }

  .db-alabel {
    font-size: 10.5px; font-weight: 600;
    letter-spacing: 0.07em; text-transform: uppercase;
    color: #94a3b8; margin-bottom: 10px;
  }
  .db-abtn {
    display: flex; align-items: center; justify-content: center; gap: 7px;
    padding: 10px 18px; border-radius: 8px;
    border: 1px solid #bfdbfe; background: #eff6ff;
    color: #2563eb; font-size: 13px;
    font-family: 'DM Sans', sans-serif; font-weight: 500;
    cursor: pointer; transition: all 0.13s; width: 100%;
  }
  .db-abtn:hover { background: #dbeafe; color: #1d4ed8; border-color: #93c5fd; }
  .db-abtn svg { width: 14px; height: 14px; }

  .db-ulabel {
    display: flex; flex-direction: column; align-items: center; gap: 6px;
    padding: 20px 14px;
    border: 1.5px dashed #cbd5e1; border-radius: 9px;
    cursor: pointer; transition: all 0.13s; background: #f8fafc;
    text-align: center; width: 100%;
  }
  .db-ulabel:hover { border-color: #93c5fd; background: #f0f7ff; }
  .db-ulabel svg { width: 20px; height: 20px; color: #94a3b8; }
  .db-ulabel span { font-size: 11.5px; color: #64748b; }
  .db-ulabel input { display: none; }
  .db-uploading {
    display: flex; align-items: center; gap: 7px;
    margin-top: 10px; font-size: 11.5px; color: #64748b;
  }
  .db-spinner {
    width: 12px; height: 12px; flex-shrink: 0;
    border: 1.5px solid #e2e8f0; border-top-color: #3b82f6;
    border-radius: 50%; animation: spin 0.7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ─── EMPTY ─── */
  .db-empty {
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: 10px;
    padding: 32px 16px; text-align: center; flex: 1;
  }
  .db-empty svg { width: 36px; height: 36px; color: #cbd5e1; }
  .db-empty p { font-size: 12px; color: #94a3b8; }
`,C=()=>e.jsx("svg",{viewBox:"0 0 16 16",fill:"none",children:e.jsx("path",{d:"M1.5 3.5A1 1 0 0 1 2.5 2.5H6l1 1.5H13a1 1 0 0 1 1 1V12a1 1 0 0 1-1 1H2.5a1 1 0 0 1-1-1V3.5z",fill:"#e8a020",opacity:"0.85"})}),z=()=>e.jsxs("svg",{viewBox:"0 0 16 16",fill:"none",children:[e.jsx("path",{d:"M3 2a1 1 0 0 1 1-1h5.586a1 1 0 0 1 .707.293l2.414 2.414A1 1 0 0 1 13 4.414V14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V2z",fill:"#4a80b0",opacity:"0.85"}),e.jsx("path",{d:"M9 1.5V4a1 1 0 0 0 1 1h2.5",stroke:"#6a9ac4",strokeWidth:"0.75",fill:"none"})]}),B=()=>e.jsx("svg",{viewBox:"0 0 12 12",fill:"none",children:e.jsx("path",{d:"M4.5 3l3 3-3 3",stroke:"currentColor",strokeWidth:"1.3",strokeLinecap:"round",strokeLinejoin:"round"})}),L=()=>e.jsx("svg",{viewBox:"0 0 14 14",fill:"none",children:e.jsx("path",{d:"M1 6.5L7 1.5l6 5M3 5.5V12a.5.5 0 0 0 .5.5h2.75v-3h1.5v3H10.5A.5.5 0 0 0 11 12V5.5",stroke:"currentColor",strokeWidth:"1.1",strokeLinecap:"round",strokeLinejoin:"round"})}),F=()=>e.jsx("svg",{viewBox:"0 0 14 14",fill:"none",children:e.jsx("path",{d:"M9 2.5L4 7l5 4.5",stroke:"currentColor",strokeWidth:"1.2",strokeLinecap:"round",strokeLinejoin:"round"})}),A=()=>e.jsx("svg",{viewBox:"0 0 14 14",fill:"none",children:e.jsx("path",{d:"M7 1.5v7M4.5 6l2.5 3 2.5-3M2.5 10.5v1a.5.5 0 0 0 .5.5h8a.5.5 0 0 0 .5-.5v-1",stroke:"currentColor",strokeWidth:"1.2",strokeLinecap:"round",strokeLinejoin:"round"})}),D=()=>e.jsx("svg",{viewBox:"0 0 20 20",fill:"none",children:e.jsx("path",{d:"M10 13V4M7.5 6.5l2.5-3 2.5 3M3.5 15v1.5a.5.5 0 0 0 .5.5h12a.5.5 0 0 0 .5-.5V15",stroke:"currentColor",strokeWidth:"1.3",strokeLinecap:"round",strokeLinejoin:"round"})}),S=()=>e.jsxs("svg",{viewBox:"0 0 20 20",fill:"none",children:[e.jsx("path",{d:"M5 3a2 2 0 0 1 2-2h6.172a2 2 0 0 1 1.414.586l2.828 2.828A2 2 0 0 1 18 5.828V17a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V3z",fill:"#bfdbfe",opacity:"0.9"}),e.jsx("path",{d:"M12 1.5V5a1 1 0 0 0 1 1h3.5",stroke:"#93c5fd",strokeWidth:"1",fill:"none"})]}),R=()=>e.jsxs("svg",{viewBox:"0 0 40 40",fill:"none",children:[e.jsx("path",{d:"M5 12a3 3 0 0 1 3-3h8l3 4h14a3 3 0 0 1 3 3v13a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3V12z",fill:"currentColor",opacity:"0.12"}),e.jsx("path",{d:"M14 22h12M17 26h6",stroke:"currentColor",strokeWidth:"1.5",strokeLinecap:"round",opacity:"0.2"})]});function E(){const[i,d]=r.useState([{path:"",name:"Root",files:[],activeItem:null}]),[s,c]=r.useState(null),[x,h]=r.useState(!1),[m,u]=r.useState(null),p=r.useRef(null);r.useEffect(()=>{b("",0)},[]),r.useEffect(()=>{p.current&&p.current.scrollTo({left:p.current.scrollWidth,behavior:"smooth"})},[i.length,s]);const b=async(l,o)=>{try{const n=await(await fetch(`${f}/list?path=${encodeURIComponent(l)}`)).json();u(!0),d(t=>{const k=t.slice(0,o),M=l?l.split("\\").filter(Boolean).pop():"PUBLIC";return k.push({path:l,name:M,files:n,activeItem:null}),k})}catch(a){console.error(a),u(!1)}},j=(l,o)=>{d(a=>a.map((n,t)=>t===l?{...n,activeItem:o.path}:n)),o.isFolder?(c(null),b(o.path,l+1)):(d(a=>a.slice(0,l+1).map((n,t)=>t===l?{...n,activeItem:o.path}:n)),c(o))},w=()=>{i.length<=1||(d(l=>l.slice(0,-1)),c(null))},y=()=>{d([{path:"",name:"Folder",files:[],activeItem:null}]),c(null),b("",0)},N=async l=>{const o=l.target.files[0];if(!o||!s)return;h(!0);const a=new FormData;a.append("file",o),a.append("path",s.path);try{await fetch(`${f}/upload-replace`,{method:"POST",body:a}),alert("File replaced successfully.");const n=i[i.length-1];b(n.path,i.length-1),c(null)}catch{alert("Upload failed.")}h(!1)},g=i.map(l=>l.name),v=m===null?{cls:"chk",label:"Checking…"}:m?{cls:"on",label:"Online"}:{cls:"off",label:"Offline"};return e.jsxs(e.Fragment,{children:[e.jsx("style",{children:I}),e.jsxs("div",{className:"db-root",children:[e.jsxs("div",{className:"db-topbar",children:[e.jsxs("div",{style:{display:"flex",alignItems:"center",gap:12},children:[e.jsxs("div",{className:"db-logo",children:[e.jsx("div",{className:"db-logo-dot"}),"Network > 192.168.1.10 > Public"]}),e.jsxs("div",{className:"db-status",children:[e.jsx("div",{className:`db-dot ${v.cls}`}),v.label]})]}),e.jsxs("div",{className:"db-nav",children:[e.jsxs("button",{className:"db-btn",onClick:y,children:[e.jsx(L,{})," Folder"]}),e.jsxs("button",{className:"db-btn",onClick:w,children:[e.jsx(F,{})," Back"]})]})]}),e.jsxs("div",{className:"db-crumb",children:[g.map((l,o)=>e.jsxs("span",{className:`db-crumb-seg${o===g.length-1&&!s?" last":""}`,onClick:()=>{o<i.length-1&&(d(a=>a.slice(0,o+1)),c(null))},children:[o>0&&e.jsx("span",{className:"db-crumb-sep",children:"›"}),e.jsx("span",{className:"db-crumb-label",children:l})]},o)),s&&e.jsxs("span",{className:"db-crumb-seg last",children:[e.jsx("span",{className:"db-crumb-sep",children:"›"}),e.jsx("span",{className:"db-crumb-label",children:s.name})]})]}),e.jsxs("div",{className:"db-columns-wrap",children:[e.jsx("div",{className:`db-columns-scroll${s?"":" no-file"}`,ref:p,children:i.map((l,o)=>e.jsxs("div",{className:"db-col",style:{zIndex:o+1},children:[e.jsx("div",{className:"db-col-header",children:e.jsx("div",{className:`db-col-title${l.path===""?" root-lbl":""}`,children:l.path===""?"/ root":l.name})}),e.jsx("div",{className:"db-col-list",children:l.files.length===0?e.jsxs("div",{className:"db-empty",children:[e.jsx(R,{}),e.jsx("p",{children:"Empty folder"})]}):l.files.map(a=>{const t=l.activeItem===a.path?a.isFolder?"db-item active-folder":"db-item active-file":"db-item";return e.jsxs("div",{className:t,onClick:()=>j(o,a),children:[e.jsx("span",{className:"db-iicon",children:a.isFolder?e.jsx(C,{}):e.jsx(z,{})}),e.jsx("span",{className:"db-iname",children:a.name}),a.isFolder&&e.jsx("span",{className:"db-chevron",children:e.jsx(B,{})})]},a.path)})})]},o))}),s&&e.jsxs("div",{className:"db-file-panel",children:[e.jsxs("div",{className:"db-panel-header",children:[e.jsx("div",{className:"db-panel-ficon",children:e.jsx(S,{})}),e.jsx("div",{className:"db-panel-name",children:s.name}),e.jsx("div",{className:"db-panel-path",children:s.path})]}),e.jsxs("div",{className:"db-panel-actions",children:[e.jsxs("div",{className:"db-panel-action-block",children:[e.jsx("div",{className:"db-alabel",children:"Open file"}),e.jsxs("button",{className:"db-abtn",onClick:()=>window.open(`${f}/file?name=${encodeURIComponent(s.path)}`,"_blank"),children:[e.jsx(A,{})," Open / Download"]})]}),e.jsxs("div",{className:"db-panel-action-block",children:[e.jsx("div",{className:"db-alabel",children:"Replace file"}),e.jsxs("label",{className:"db-ulabel",children:[e.jsx(D,{}),e.jsx("span",{children:"Click to upload replacement"}),e.jsx("input",{type:"file",onChange:N,disabled:x})]}),x&&e.jsxs("div",{className:"db-uploading",children:[e.jsx("div",{className:"db-spinner"})," Uploading…"]})]})]})]})]})]})]})}export{E as default};
