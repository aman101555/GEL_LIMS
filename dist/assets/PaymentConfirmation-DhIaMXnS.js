import{r as o,A as s,j as e}from"./index-C4mzkx3I.js";const X=`
  .pc-wrap { font-family: 'Segoe UI', sans-serif; padding: 28px; background: #f8f9fb; min-height: 100vh; }
  .pc-header { margin-bottom: 24px; }
  .pc-header h2 { font-size: 30px; font-weight: 700; color: #111827; margin: 0 0 4px; }
  .pc-header p { color: #6b7280; font-size: 13px; margin: 0; }

  .pc-filters { display: flex; gap: 8px; margin-bottom: 24px; flex-wrap: wrap; }
  .pc-filter-btn {
    padding: 8px 20px; border-radius: 20px; border: 1.5px solid #e5e7eb;
    background: white; font-size: 14px; font-weight: 600; cursor: pointer;
    color: #6b7280; transition: all 0.18s ease;
  }
  .pc-filter-btn:hover { border-color: #2563eb; color: #2563eb; }
  .pc-filter-btn.active { background: #2563eb; border-color: #2563eb; color: white; box-shadow: 0 2px 8px rgba(37,99,235,0.25); }
  .pc-filter-btn.verified.active { background: #059669; border-color: #059669; box-shadow: 0 2px 8px rgba(5,150,105,0.25); }

  .pc-card { background: white; border-radius: 12px; box-shadow: 0 1px 4px rgba(0,0,0,0.07); overflow: hidden; }
  .pc-table { width: 100%; border-collapse: collapse; }
  .pc-table th { background: #f9fafb; padding: 12px 16px; text-align: left; font-size: 12px; font-weight: 700; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid #f0f0f0; }
  .pc-table td { padding: 13px 16px; font-size: 13.5px; color: #374151; border-bottom: 1px solid #f9fafb; vertical-align: middle; }
  .pc-table tr:last-child td { border-bottom: none; }
  .pc-table tr:hover td { background: #fafafa; }

  .badge { display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 11.5px; font-weight: 700; }
  .badge-paid { background: #d1fae5; color: #065f46; }
  .badge-unpaid { background: #fef3c7; color: #92400e; }
  .badge-cash { background: #dbeafe; color: #1e40af; }
  .badge-credit { background: #ede9fe; color: #4c1d95; }
  .badge-verified { background: #d1fae5; color: #065f46; }

  .btn { display: inline-flex; align-items: center; gap: 5px; padding: 6px 14px; border-radius: 6px; border: 1.5px solid; font-size: 12.5px; font-weight: 600; cursor: pointer; transition: all 0.15s; }
  .btn-upload { background: #eff6ff; border-color: #bfdbfe; color: #2563eb; }
  .btn-upload:hover { background: #dbeafe; }
  .btn-view { background: #f0fdf4; border-color: #bbf7d0; color: #16a34a; }
  .btn-view:hover { background: #dcfce7; }
  .btn-verify { background: white; border-color: #e5e7eb; color: #374151; padding: 6px 10px; }
  .btn-verify:hover { border-color: #16a34a; color: #16a34a; }
  .btn-verify.verified { color: #16a34a; border-color: #16a34a; background: #f0fdf4; cursor: default; }

  .pc-empty { text-align: center; padding: 56px 20px; color: #9ca3af; }
  .pc-empty svg { margin-bottom: 12px; opacity: 0.4; }
  .pc-empty p { font-size: 14px; margin: 0; }

  .pc-loading { text-align: center; padding: 48px; color: #6b7280; font-size: 14px; }

  .pc-error { background: #fef2f2; border: 1px solid #fecaca; color: #dc2626; padding: 12px 16px; border-radius: 8px; font-size: 13px; margin-bottom: 16px; }

  /* Modal */
  .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.5); display: flex; align-items: center; justify-content: center; z-index: 9999; }
  .modal-box { background: white; border-radius: 12px; padding: 28px; width: 420px; max-width: 95vw; box-shadow: 0 20px 60px rgba(0,0,0,0.2); }
  .modal-box h3 { margin: 0 0 6px; font-size: 17px; color: #111827; }
  .modal-box p { margin: 0 0 20px; font-size: 13px; color: #6b7280; }
  .modal-drop { border: 2px dashed #d1d5db; border-radius: 8px; padding: 32px 20px; text-align: center; cursor: pointer; transition: all 0.2s; background: #f9fafb; }
  .modal-drop:hover, .modal-drop.drag { border-color: #2563eb; background: #eff6ff; }
  .modal-drop input { display: none; }
  .modal-drop p { margin: 8px 0 0; font-size: 13px; color: #6b7280; }
  .modal-drop .file-name { color: #2563eb; font-weight: 600; font-size: 13px; margin-top: 8px; }
  .modal-actions { display: flex; gap: 10px; margin-top: 20px; }
  .modal-actions button { flex: 1; padding: 10px; border-radius: 7px; font-size: 13.5px; font-weight: 600; cursor: pointer; border: none; }
  .modal-cancel { background: #f3f4f6; color: #374151; }
  .modal-cancel:hover { background: #e5e7eb; }
  .modal-submit { background: #2563eb; color: white; }
  .modal-submit:hover { background: #1d4ed8; }
  .modal-submit:disabled { background: #93c5fd; cursor: not-allowed; }

  .progress-bar { height: 4px; background: #e5e7eb; border-radius: 4px; margin-top: 12px; overflow: hidden; }
  .progress-fill { height: 100%; background: #2563eb; border-radius: 4px; transition: width 0.3s; }

  /* Superadmin Auth Modal */
  .auth-modal-box { background: white; border-radius: 14px; padding: 28px; width: 380px; max-width: 95vw; box-shadow: 0 20px 60px rgba(0,0,0,0.25); }
  .auth-modal-icon { width: 48px; height: 48px; background: #fef3c7; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 22px; margin: 0 auto 14px; }
  .auth-modal-box h3 { margin: 0 0 4px; font-size: 16px; font-weight: 700; color: #111827; text-align: center; }
  .auth-modal-box .auth-subtitle { text-align: center; font-size: 12.5px; color: #6b7280; margin: 0 0 20px; }
  .auth-field { margin-bottom: 13px; }
  .auth-field label { display: block; font-size: 12px; font-weight: 600; color: #374151; margin-bottom: 5px; }
  .auth-field input { width: 100%; padding: 9px 12px; border: 1.5px solid #e5e7eb; border-radius: 7px; font-size: 13.5px; outline: none; box-sizing: border-box; transition: border-color 0.15s; }
  .auth-field input:focus { border-color: #2563eb; }
  .auth-error { background: #fef2f2; border: 1px solid #fecaca; color: #dc2626; padding: 9px 12px; border-radius: 7px; font-size: 12.5px; margin-bottom: 13px; text-align: center; }
  .auth-actions { display: flex; gap: 10px; margin-top: 6px; }
  .auth-actions button { flex: 1; padding: 10px; border-radius: 7px; font-size: 13.5px; font-weight: 600; cursor: pointer; border: none; transition: background 0.15s; }
  .auth-cancel { background: #f3f4f6; color: #374151; }
  .auth-cancel:hover { background: #e5e7eb; }
  .auth-submit { background: #2563eb; color: white; }
  .auth-submit:hover { background: #1d4ed8; }
  .auth-submit:disabled { background: #93c5fd; cursor: not-allowed; }
`;function q(){const[i,L]=o.useState("ALL"),[P,V]=o.useState([]),[$,F]=o.useState([]),[R,w]=o.useState(!0),[k,S]=o.useState(""),[p,u]=o.useState(null),[d,l]=o.useState(null),[c,g]=o.useState(!1),[N,n]=o.useState(0),[I,m]=o.useState(!1),z=o.useRef(),T=(JSON.parse(localStorage.getItem("user")||"{}").role||"").toLowerCase()==="super_admin",[_,f]=o.useState(null),[y,C]=o.useState(""),[j,D]=o.useState(""),[E,h]=o.useState(""),[b,A]=o.useState(!1),M=[...new Set($.map(t=>t.payment_terms).filter(Boolean))],O=[{key:"ALL",label:"ALL"},...M.map(t=>({key:t,label:t})),{key:"VERIFIED",label:"✓ Verified Quotations"}];o.useEffect(()=>{fetch(`${s}/payments/`).then(t=>t.json()).then(t=>F(t)).catch(()=>{})},[]),o.useEffect(()=>{x()},[i]);const x=async()=>{w(!0),S("");try{const t=new URLSearchParams;i!=="ALL"&&t.append("filter",i);const a=await fetch(`${s}/payments/?${t}`);if(!a.ok)throw new Error("Failed to fetch quotations");const r=await a.json();V(r)}catch(t){S(t.message)}finally{w(!1)}},Q=t=>{T?B(t):(f({quotation_id:t}),C(""),D(""),h(""))},B=async t=>{try{const a=await fetch(`${s}/payments/${t}/verify`,{method:"POST"});if(!a.ok){const r=await a.json();throw new Error(r.detail||"Verification failed")}x()}catch(a){alert(a.message)}},v=async()=>{if(!y.trim()||!j.trim()){h("Please enter both username and password.");return}A(!0),h("");try{const t=await fetch(`${s}/payments/${_.quotation_id}/verify-with-credentials`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username:y,password:j})}),a=await t.json();if(!t.ok)throw new Error(a.detail||"Verification failed");f(null),x()}catch(t){h(t.message)}finally{A(!1)}},G=async(t,a)=>{try{const r=await fetch(`${s}/payments/${t}/receipt-url`);if(!r.ok)throw new Error("Could not get receipt URL");const H=await r.json();window.open(H.url,"_blank")}catch(r){alert(r.message)}},J=t=>{t.preventDefault(),m(!1);const a=t.dataTransfer.files[0];a&&l(a)},K=async()=>{if(!(!d||!p)){g(!0),n(10);try{const t=new FormData;t.append("file",d),n(40);const a=await fetch(`${s}/payments/${p.quotation_id}/upload-receipt`,{method:"POST",body:t});if(n(90),!a.ok){const r=await a.json();throw new Error(r.detail||"Upload failed")}n(100),setTimeout(()=>{u(null),l(null),g(!1),n(0),x()},600)}catch(t){alert(t.message),g(!1),n(0)}}},U=P,W=()=>{if(R)return e.jsx("div",{className:"pc-loading",children:"Loading..."});if(!U.length)return e.jsxs("div",{className:"pc-empty",children:[e.jsx("svg",{width:"48",height:"48",fill:"none",stroke:"currentColor",strokeWidth:"1.5",viewBox:"0 0 24 24",children:e.jsx("path",{strokeLinecap:"round",strokeLinejoin:"round",d:"M9 12h6m-3-3v6M4.5 6.375a4.125 4.125 0 118.25 0 4.125 4.125 0 01-8.25 0zM14.25 8.625a3.375 3.375 0 116.75 0 3.375 3.375 0 01-6.75 0z"})}),e.jsx("p",{children:"No quotations found for this filter."})]});const t=i==="VERIFIED";return e.jsxs("table",{className:"pc-table",children:[e.jsx("thead",{children:e.jsxs("tr",{children:[e.jsx("th",{children:"Quotation No"}),e.jsx("th",{children:"Client"}),e.jsx("th",{children:"Payment Type"}),e.jsx("th",{children:"Payment Details"}),e.jsx("th",{children:"Status"}),e.jsx("th",{children:"Receipt"}),!t&&e.jsx("th",{children:"Verify"}),t&&e.jsx("th",{children:"Verified On"})]})}),e.jsx("tbody",{children:U.map(a=>e.jsxs("tr",{children:[e.jsx("td",{children:e.jsx("strong",{children:a.quotation_no})}),e.jsx("td",{children:a.client_name||"—"}),e.jsx("td",{children:e.jsx("span",{className:`badge ${a.payment_terms?.toUpperCase().includes("CASH")?"badge-cash":"badge-credit"}`,children:a.payment_terms||"—"})}),e.jsx("td",{children:e.jsxs("div",{style:{fontSize:12,color:"#374151"},children:[e.jsxs("div",{children:["AED ",parseFloat(a.grand_total||0).toLocaleString("en-AE",{minimumFractionDigits:2})]}),e.jsx("div",{style:{color:"#9ca3af",fontSize:11},children:a.division})]})}),e.jsx("td",{children:e.jsx("span",{className:`badge ${a.receipt_path?"badge-paid":"badge-unpaid"}`,children:a.receipt_path?"PAID":"UNPAID"})}),e.jsx("td",{children:a.receipt_path?e.jsx("button",{className:"btn btn-view",onClick:()=>G(a.quotation_id,a.receipt_path),children:"👁 View"}):e.jsx("button",{className:"btn btn-upload",onClick:()=>u({quotation_id:a.quotation_id,quotation_no:a.quotation_no}),children:"Upload"})}),!t&&e.jsx("td",{children:e.jsx("button",{className:`btn btn-verify ${a.verified_at?"verified":""}`,onClick:()=>!a.verified_at&&Q(a.quotation_id),disabled:!a.receipt_path,title:a.receipt_path?a.verified_at?"Already verified":"Mark as verified":"Upload a receipt first",children:a.verified_at?"✓ Verified":"Verify"})}),t&&e.jsx("td",{style:{fontSize:12.5,color:"#374151"},children:a.verified_at?new Date(a.verified_at).toLocaleDateString("en-GB"):"—"})]},a.quotation_id))})]})};return e.jsxs(e.Fragment,{children:[e.jsx("style",{children:X}),e.jsxs("div",{className:"pc-wrap",children:[e.jsxs("div",{className:"pc-header",children:[e.jsx("h2",{children:"Payment Confirmation"}),e.jsx("p",{children:"Manage payment receipts and verify quotations before converting to projects."})]}),k&&e.jsxs("div",{className:"pc-error",children:["⚠️ ",k]}),e.jsx("div",{className:"pc-filters",children:O.map(t=>e.jsx("button",{className:`pc-filter-btn ${i===t.key?"active":""} ${t.key==="VERIFIED"&&i==="VERIFIED"?"verified":""}`,onClick:()=>L(t.key),children:t.label},t.key))}),e.jsx("div",{className:"pc-card",children:W()}),e.jsx("p",{style:{marginTop:12,fontSize:12,color:"#9ca3af"},children:"ℹ️ Only verified quotations can be converted into projects. A receipt must be uploaded before verification."})]}),p&&e.jsx("div",{className:"modal-overlay",onClick:t=>{t.target===t.currentTarget&&!c&&(u(null),l(null))},children:e.jsxs("div",{className:"modal-box",children:[e.jsx("h3",{children:"Upload Receipt"}),e.jsxs("p",{children:["Quotation: ",e.jsx("strong",{children:p.quotation_no})]}),e.jsxs("div",{className:`modal-drop ${I?"drag":""}`,onDragOver:t=>{t.preventDefault(),m(!0)},onDragLeave:()=>m(!1),onDrop:J,onClick:()=>z.current?.click(),children:[e.jsx("input",{ref:z,type:"file",accept:"image/*,.pdf",onChange:t=>l(t.target.files[0])}),e.jsx("div",{style:{fontSize:28},children:"📄"}),d?e.jsxs("p",{className:"file-name",children:["✓ ",d.name]}):e.jsxs("p",{children:["Drag & drop or click to choose",e.jsx("br",{}),e.jsx("span",{style:{fontSize:11,color:"#aaa"},children:"PDF, JPG, PNG accepted"})]})]}),c&&e.jsx("div",{className:"progress-bar",style:{marginTop:14},children:e.jsx("div",{className:"progress-fill",style:{width:`${N}%`}})}),e.jsxs("div",{className:"modal-actions",children:[e.jsx("button",{className:"modal-cancel",onClick:()=>{u(null),l(null)},disabled:c,children:"Cancel"}),e.jsx("button",{className:"modal-submit",onClick:K,disabled:!d||c,children:c?`Uploading... ${N}%`:"Upload Receipt"})]})]})}),_&&e.jsx("div",{className:"modal-overlay",onClick:t=>{t.target===t.currentTarget&&!b&&f(null)},children:e.jsxs("div",{className:"auth-modal-box",children:[e.jsx("div",{className:"auth-modal-icon",children:"🔐"}),e.jsx("h3",{children:"Superadmin Authorisation Required"}),e.jsxs("p",{className:"auth-subtitle",children:["Only Super Admins can verify quotations.",e.jsx("br",{}),"Enter superadmin credentials to proceed."]}),E&&e.jsxs("div",{className:"auth-error",children:["⚠️ ",E]}),e.jsxs("div",{className:"auth-field",children:[e.jsx("label",{children:"Username"}),e.jsx("input",{type:"text",placeholder:"Superadmin username",value:y,onChange:t=>C(t.target.value),onKeyDown:t=>t.key==="Enter"&&v(),autoFocus:!0})]}),e.jsxs("div",{className:"auth-field",children:[e.jsx("label",{children:"Password"}),e.jsx("input",{type:"password",placeholder:"Superadmin password",value:j,onChange:t=>D(t.target.value),onKeyDown:t=>t.key==="Enter"&&v()})]}),e.jsxs("div",{className:"auth-actions",children:[e.jsx("button",{className:"auth-cancel",onClick:()=>f(null),disabled:b,children:"Cancel"}),e.jsx("button",{className:"auth-submit",onClick:v,disabled:b,children:b?"Verifying...":"Verify Quotation"})]})]})})]})}export{q as default};
