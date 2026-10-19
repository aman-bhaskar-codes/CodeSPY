import {test} from 'node:test';import assert from 'node:assert/strict';import {analyze} from '../src/analyzer';
test('config is data and dependencies unavailable',()=>{const r=analyze({schema_version:1,files:[{path:'main.ts',text:'import {readFile} from "node:fs"; readFile("x");'}],configs:[{path:'tsconfig.json',text:'{"extends":"/etc/passwd","compilerOptions":{"plugins":[{"name":"evil"}]}}'}]});assert.ok(r.diagnostics.some(x=>x.code==='external_extends_forbidden'));assert.ok(r.diagnostics.some(x=>x.code==='plugins_forbidden'));assert.ok(r.resolutions.every(x=>x.candidates.every(c=>c.path==='main.ts')));});
test('config cycles bounded',()=>{const r=analyze({schema_version:1,files:[],configs:[{path:'a.json',text:'{"extends":"./b.json"}'},{path:'b.json',text:'{"extends":"./a.json"}'}]});assert.ok(r.diagnostics.some(x=>x.code==='config_cycle_or_depth'));});
test('compiler host cannot read host filesystem or node_modules',()=>{
 // Only bootstrap may read packaged libraries. A target-shaped read is a test failure.
 const fs=require('node:fs') as typeof import('node:fs');const original=fs.readFileSync;const reads:string[]=[];
 fs.readFileSync=((p:unknown,...args:unknown[])=>{reads.push(String(p));assert.match(String(p),/typescript\/lib\/lib\.[a-z0-9.]+\.d\.ts$/);return (original as Function)(p,...args);}) as typeof fs.readFileSync;
 try {const r=analyze({schema_version:1,files:[{path:'a.ts',text:'import x from "/etc/passwd"; import y from "host-only-package"; x(); y();'}]});assert.ok(r.resolutions.filter(x=>x.resolution_kind==='call_target').every(x=>x.status==='unresolved'));assert.ok(reads.length>0);}finally{fs.readFileSync=original;}
});
