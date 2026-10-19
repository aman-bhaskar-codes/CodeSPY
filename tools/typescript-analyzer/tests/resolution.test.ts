import {test} from 'node:test';import assert from 'node:assert/strict';import {analyze} from '../src/analyzer';
test('UTF16 CRLF TSX and aliases retain compiler declarations',()=>{const text='// 😀\r\nimport {f} from "@/lib";\r\nf(); const view=<div/>;';const r=analyze({schema_version:1,files:[{path:'main.tsx',text},{path:'lib.ts',text:'export function f(){}'}],configs:[{path:'tsconfig.json',text:'{"compilerOptions":{"baseUrl":".","paths":{"@/*":["*"]}}}'}]});const call=r.resolutions.find(x=>x.resolution_kind==='call_target');assert.equal(text.slice(call!.start,call!.end),'f');assert.equal(call?.status,'resolved');assert.ok(r.resolutions.some(x=>x.candidates.some(c=>c.path==='lib.ts')));});
test('overloads retained; variable calls abstain',()=>{const r=analyze({schema_version:1,files:[{path:'a.ts',text:'function f(x:string):void; function f(x:any){}; f("x"); const g=f;g("x");'}]});const calls=r.resolutions.filter(x=>x.resolution_kind==='call_target');assert.equal(calls[0].candidates.length,2);assert.equal(calls[1].status,'unresolved');});
test('files/include/exclude select independent admitted project roots',()=>{
 const files=[{path:'src/a.ts',text:'const a=1; a;'},{path:'src/b.ts',text:'const b=2; b;'},{path:'other.ts',text:'const other=3;other;'}];
 const selected=analyze({schema_version:1,files,configs:[{path:'tsconfig.json',text:'{"files":["src/a.ts"]}'}]});
 assert.deepEqual([...new Set(selected.resolutions.map(r=>r.path))],['src/a.ts']);
 const included=analyze({schema_version:1,files,configs:[{path:'tsconfig.json',text:'{"include":["src/**/*.ts"],"exclude":["src/b.ts"]}'}]});
 assert.deepEqual([...new Set(included.resolutions.map(r=>r.path))],['src/a.ts']);
});
test('conflicting project aliases retain multiple compiler bindings',()=>{
 const r=analyze({schema_version:1,files:[{path:'shared.ts',text:'import {f} from "alias"; f();'},{path:'one.ts',text:'export function f(){}'},{path:'two.ts',text:'export function f(){}'}],configs:[{path:'one.json',text:'{"files":["shared.ts"],"compilerOptions":{"baseUrl":".","paths":{"alias":["one.ts"]}}}'},{path:'two.json',text:'{"files":["shared.ts"],"compilerOptions":{"baseUrl":".","paths":{"alias":["two.ts"]}}}'}]});
 const call=r.resolutions.find(x=>x.path==='shared.ts'&&x.resolution_kind==='call_target');assert.equal(call?.status,'ambiguous');assert.deepEqual(call?.candidates.map(c=>c.path),['one.ts','two.ts']);
});
test('parameter lexical binding does not invent its runtime argument target',()=>{
 const r=analyze({schema_version:1,files:[{path:'a.ts',text:'function target() {} function invoke(cb:()=>void){cb();} invoke(target);'}]});
 const cb=r.resolutions.find(x=>x.resolution_kind==='call_target'&&x.start==='function target() {} function invoke(cb:()=>void){cb();} invoke(target);'.indexOf('cb();'));assert.equal(cb?.status,'unresolved');
 assert.ok(r.resolutions.some(x=>x.resolution_kind==='binding'&&x.candidates.some(c=>c.name==='cb')));
});
test('invalid project context cannot silently select the remaining alias',()=>{
 const r=analyze({schema_version:1,files:[{path:'main.ts',text:'function f(){};f();'}],configs:[{path:'good.json',text:'{"files":["main.ts"]}'},{path:'bad.json',text:'{"files":["main.ts"],"extends":"external-package"}'}]});
 assert.ok(r.resolutions.length>0);assert.ok(r.resolutions.every(x=>x.status==='unresolved'&&x.reason==='incomplete_project_context'));
});
