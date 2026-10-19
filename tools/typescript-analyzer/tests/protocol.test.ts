import {test} from 'node:test';import assert from 'node:assert/strict';import {validate} from '../src/analyzer';
test('strict bounds and paths',()=>{for(const path of ['../x.ts','/x.ts','a\\b.ts','a/../x'])assert.throws(()=>validate({schema_version:1,files:[{path,text:''}]}));assert.throws(()=>validate({schema_version:1,files:[],max_sites:2001}));});
