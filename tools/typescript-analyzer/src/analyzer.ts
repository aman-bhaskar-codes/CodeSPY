import ts from 'typescript';
import fs from 'node:fs';
import path from 'node:path';
export interface Input {schema_version:1;files:{path:string;text:string}[];configs?:{path:string;text:string}[];max_sites?:number}
export interface Candidate {path:string;start:number;end:number;name:string}
export interface Resolution {path:string;start:number;end:number;namespace:'value'|'type';resolution_kind:'binding'|'call_target'|'import';status:'resolved'|'ambiguous'|'unresolved';candidates:Candidate[];reason:string;basis:'compiler_symbol'}
export function safePath(p:unknown): p is string {return typeof p==='string' && p.length<=1024 && !p.includes('\\') && !p.includes('\0') && !p.startsWith('/') && !p.startsWith('__trusted/') && p.split('/').every(x=>!!x && x!=='.' && x!=='..');}
export function validate(value:unknown):Input {
 const x=value as Input;if(!x||x.schema_version!==1||!Array.isArray(x.files)||Object.keys(x).some(k=>!['schema_version','files','configs','max_sites'].includes(k)))throw Error('invalid input');
 if(x.configs!==undefined&&!Array.isArray(x.configs))throw Error('invalid configs');
 const entries=[...x.files,...(x.configs??[])];if(entries.length>128)throw Error('file count');let bytes=0;const names=new Set<string>();
 for(const f of entries){if(!f||!safePath(f.path)||typeof f.text!=='string'||Object.keys(f).sort().join(',')!=='path,text'||names.has(f.path))throw Error('invalid file');names.add(f.path);const n=Buffer.byteLength(f.text);bytes+=n;if(n>2*1024*1024)throw Error('file bytes');}
 if(bytes>16*1024*1024||x.max_sites!==undefined&&(!Number.isInteger(x.max_sites)||x.max_sites<1||x.max_sites>2000))throw Error('budget');return x;
}
export function analyze(value:unknown) {
 const input=validate(value);const files=new Map(input.files.map(f=>['/'+f.path,f.text]));const configs=new Map((input.configs??[]).map(f=>['/'+f.path,f.text]));
 const diagnostics:{code:string;message:string;path?:string}[]=[];const diag=(code:string,p?:string)=>{if(diagnostics.length<128)diagnostics.push({code,message:code,...(p?{path:p.slice(1)}:{})});};
 const libRoot=path.dirname(require.resolve('typescript/lib/lib.es2022.d.ts'));const libs=new Map<string,string>();
 for(const name of fs.readdirSync(libRoot)){if(/^lib\.[a-z0-9.]+\.d\.ts$/.test(name))libs.set('/__trusted/'+name,fs.readFileSync(path.join(libRoot,name),'utf8'));}
 const all=new Map([...files,...libs]);
 const exists=(p:string)=>all.has(p);const read=(p:string)=>all.get(p);const dir=(p:string)=>[...all.keys()].some(k=>k.startsWith(p.replace(/\/$/,'')+'/'));
 const host:ts.CompilerHost={getSourceFile:(p,t)=>{const text=read(p);return text===undefined?undefined:ts.createSourceFile(p,text,t,true);},getDefaultLibFileName:()=>'/__trusted/lib.es2022.d.ts',writeFile:()=>{throw Error('emit forbidden');},getCurrentDirectory:()=> '/',getDirectories:()=>[],fileExists:exists,readFile:read,directoryExists:dir,realpath:p=>p,readDirectory:(root,extensions)=>[...files.keys()].filter(p=>p.startsWith(root.replace(/\/$/,'')+'/')&&(!extensions||extensions.some(e=>p.endsWith(e)))),useCaseSensitiveFileNames:()=>true,getCanonicalFileName:p=>p,getNewLine:()=> '\n'};
 const defaults:ts.CompilerOptions={target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext,moduleResolution:ts.ModuleResolutionKind.Bundler,jsx:ts.JsxEmit.Preserve,noEmit:true,incremental:false,allowJs:true,types:[],skipLibCheck:true};
 type Project={options:ts.CompilerOptions;roots:string[];valid:boolean};
 const projects=new Map<string,Project>();const bases=new Set<string>();let refs=0;
 function local(p:string,s:unknown):string|undefined {if(typeof s!=='string'||s.startsWith('/')||s.split('/').includes('..')||s.includes('\\'))return;let q=path.posix.join(path.posix.dirname(p),s);if(configs.has(q))return q;if(configs.has(q+'.json'))return q+'.json';if(configs.has(q+'/tsconfig.json'))return q+'/tsconfig.json';}
 function pattern(base:string,glob:string):RegExp {let g=path.posix.join(base,glob);if(!/[.*?]/.test(path.posix.basename(g)))g+='/**/*';let out='^';for(let i=0;i<g.length;i++){const c=g[i];if(c==='*'&&g[i+1]==='*'){i++;if(g[i+1]==='/'){i++;out+='(?:.*/)?';}else out+='.*';}else if(c==='*')out+='[^/]*';else if(c==='?')out+='[^/]';else out+=c.replace(/[|\\{}()[\]^$+?.]/g,'\\$&');}return new RegExp(out+'$');}
 function config(p:string,stack:string[]=[]):Project {
  const invalid=():Project=>({options:defaults,roots:[],valid:false});
  if(stack.length>=8||stack.includes(p)){diag('config_cycle_or_depth',p);return invalid();}const cached=projects.get(p);if(cached)return cached;
  const raw=configs.get(p);if(raw===undefined){diag('unavailable_config',p);return invalid();}const parsed=ts.parseConfigFileTextToJson(p,raw);if(parsed.error){diag('invalid_jsonc',p);return invalid();}const c=parsed.config;if(!c||typeof c!=='object'||Array.isArray(c)){diag('invalid_config',p);return invalid();}
  let parent:Project|undefined;let valid=true;
  if(c.extends!==undefined){const q=local(p,c.extends);if(q){bases.add(q);parent=config(q,[...stack,p]);valid=parent.valid;}else{diag('external_extends_forbidden',p);valid=false;}}
  if(c.compilerOptions?.plugins){diag('plugins_forbidden',p);valid=false;}
  const allowed=['baseUrl','paths','module','moduleResolution','jsx','strict','checkJs','allowSyntheticDefaultImports','esModuleInterop'];const picked:Record<string,unknown>={};for(const key of allowed)if(c.compilerOptions?.[key]!==undefined)picked[key]=c.compilerOptions[key];
  const safeRelative=(v:unknown)=>typeof v==='string'&&!v.startsWith('/')&&!v.includes('\\')&&!v.split('/').includes('..');
  if(picked.baseUrl!==undefined&&!safeRelative(picked.baseUrl)){diag('external_base_url',p);valid=false;delete picked.baseUrl;}
  if(picked.paths){if(typeof picked.paths!=='object'||Array.isArray(picked.paths)||Object.values(picked.paths).some(v=>!Array.isArray(v)||v.some(x=>!safeRelative(x)))){diag('external_paths',p);valid=false;delete picked.paths;}}
  const converted=ts.convertCompilerOptionsFromJson(picked,path.posix.dirname(p));if(converted.errors.length){diag('invalid_options',p);valid=false;}
  const options={...defaults,...parent?.options,...converted.options,noEmit:true,incremental:false,allowJs:true,types:[]};
  let roots:string[]=[];const base=path.posix.dirname(p);
  for(const key of ['files','include','exclude'])if(c[key]!==undefined&&(!Array.isArray(c[key])||c[key].some((v:unknown)=>!safeRelative(v)))){diag('invalid_selection',p);valid=false;}
  if(valid){
   if(c.files!==undefined){roots=c.files.map((v:string)=>path.posix.join(base,v)).filter((v:string)=>{if(!files.has(v)){diag('unavailable_source',p);return false;}return true;});}
   else if(c.include===undefined&&parent)roots=[...parent.roots];
   else {const includes=(c.include??['**/*']).map((g:string)=>pattern(base,g));const excludes=(c.exclude??['node_modules','bower_components','jspm_packages']).map((g:string)=>pattern(base,g));roots=[...files.keys()].filter(f=>/\.(?:[cm]?[jt]sx?)$/.test(f)&&includes.some((r:RegExp)=>r.test(f))&&!excludes.some((r:RegExp)=>r.test(f)));}
  }
  if(valid&&c.files!==undefined&&c.include!==undefined){const includes=c.include.map((g:string)=>pattern(base,g));const excludes=(c.exclude??[]).map((g:string)=>pattern(base,g));roots=[...new Set([...roots,...[...files.keys()].filter(f=>/\.(?:[cm]?[jt]sx?)$/.test(f)&&includes.some((r:RegExp)=>r.test(f))&&!excludes.some((r:RegExp)=>r.test(f)))])];}
  if(c.references!==undefined){if(!Array.isArray(c.references)){diag('invalid_references',p);valid=false;}else for(const r of c.references){if(++refs>16){diag('reference_limit',p);valid=false;break;}const q=local(p,r?.path);if(q)config(q,[...stack,p]);else{diag('unavailable_reference',p);valid=false;}}}
  const project={options,roots:roots.sort(),valid};projects.set(p,project);return project;
 }
 for(const p of [...configs.keys()].sort())config(p);
 const contexts=configs.size?[...projects.entries()].filter(([p,v])=>!bases.has(p)&&v.valid).map(([,v])=>v):[{options:defaults,roots:[...files.keys()].sort(),valid:true}];
 const resolutions:Resolution[]=[];let truncated=false;const limit=input.max_sites??2000;
 let checker:ts.TypeChecker;
 function record(n:ts.Node,symbol:ts.Symbol|undefined,kind:Resolution['resolution_kind'],namespace:'value'|'type',reason?:string){if(resolutions.length>=limit){truncated=true;return;}const sf=n.getSourceFile();let candidates:Candidate[]=[];
  if(symbol&&!reason){if(symbol.flags&ts.SymbolFlags.Alias){try{symbol=checker.getAliasedSymbol(symbol);}catch{symbol=undefined;}}for(const d of (symbol?.declarations??[]).slice(0,128)){const f=d.getSourceFile();if(files.has(f.fileName)){const named=(d as ts.NamedDeclaration).name??d;candidates.push({path:f.fileName.slice(1),start:named.getStart(f),end:named.end,name:symbol!.getName()});}}}
  if((symbol?.declarations?.length??0)>128){truncated=true;candidates=[];reason='declaration_limit';}
  candidates=candidates.filter((c,i,a)=>a.findIndex(x=>JSON.stringify(x)===JSON.stringify(c))===i).sort((a,b)=>a.path.localeCompare(b.path)||a.start-b.start);
  resolutions.push({path:sf.fileName.slice(1),start:n.getStart(sf),end:n.end,namespace,resolution_kind:kind,status:candidates.length>1?'ambiguous':candidates.length?'resolved':'unresolved',candidates,reason:reason??(candidates.length?'static_symbol':'declaration_unavailable'),basis:'compiler_symbol'});
 }
 for(const context of contexts){const program=ts.createProgram(context.roots,context.options,host);checker=program.getTypeChecker();
 for(const p of [...files.keys()].sort()){const sf=program.getSourceFile(p);if(!sf)continue;function visit(n:ts.Node){if(truncated)return;
  if(ts.isIdentifier(n)){let parent:ts.Node|undefined=n.parent;let type=false;while(parent&&!ts.isStatement(parent)){if(ts.isTypeNode(parent)){type=true;break;}parent=parent.parent;}record(n,checker.getSymbolAtLocation(n),'binding',type?'type':'value');}
  if(ts.isCallExpression(n)){const expr=n.expression;let sym=checker.getSymbolAtLocation(expr);const typeOnly=(sym?.declarations??[]).some(d=>{let x:ts.Node|undefined=d;while(x&&!ts.isSourceFile(x)){if((ts.isImportSpecifier(x)||ts.isImportClause(x)||ts.isExportSpecifier(x))&&x.isTypeOnly)return true;x=x.parent;}return false;});if(sym&&(sym.flags&ts.SymbolFlags.Alias))sym=checker.getAliasedSymbol(sym);const declarations=sym?.declarations??[];const direct=!typeOnly&&declarations.length>0&&declarations.every(d=>ts.isFunctionDeclaration(d));record(expr,direct?sym:undefined,'call_target','value',direct?undefined:'dynamic_call_target');}
  if(ts.isStringLiteral(n)&&(ts.isImportDeclaration(n.parent)||ts.isExportDeclaration(n.parent)))record(n,checker.getSymbolAtLocation(n),'import',ts.isImportDeclaration(n.parent)&&n.parent.importClause?.isTypeOnly?'type':'value');
  ts.forEachChild(n,visit);
 }visit(sf);}}
 const merged=new Map<string,Resolution>();for(const row of resolutions){const key=JSON.stringify([row.path,row.start,row.end,row.namespace,row.resolution_kind]);const prior=merged.get(key);if(!prior){merged.set(key,row);continue;}const mismatch=prior.status==='unresolved'||row.status==='unresolved';prior.candidates=[...new Map([...prior.candidates,...row.candidates].map(c=>[JSON.stringify(c),c])).values()].sort((a,b)=>a.path.localeCompare(b.path)||a.start-b.start);if(mismatch&&prior.candidates.length){prior.candidates=[];prior.status='unresolved';prior.reason='incomplete_project_context';}else{prior.status=prior.candidates.length>1?'ambiguous':prior.candidates.length?'resolved':'unresolved';if(prior.status==='ambiguous')prior.reason='multiple_project_bindings';}}
 if((truncated&&contexts.length>1)||diagnostics.length)for(const row of merged.values()){row.candidates=[];row.status='unresolved';row.reason=diagnostics.length?'incomplete_project_context':'project_budget_incomplete';}
 return {schema_version:1 as const,resolutions:[...merged.values()],diagnostics,truncated};
}
