import {analyze} from './analyzer';
const chunks:Buffer[]=[];let bytes=0;
process.stdin.on('data',(chunk:Buffer)=>{bytes+=chunk.length;if(bytes>20*1024*1024){process.stderr.write('input limit\n');process.exit(2);}chunks.push(chunk);});
process.stdin.on('end',()=>{try{const output=JSON.stringify(analyze(JSON.parse(Buffer.concat(chunks).toString('utf8'))));if(Buffer.byteLength(output)>8*1024*1024)throw Error('output limit');process.stdout.write(output);}catch{process.stderr.write('invalid semantic request\n');process.exitCode=2;}});
