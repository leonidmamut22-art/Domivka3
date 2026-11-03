import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
const html=await readFile('webapp/index.html','utf8');
for(const [i,match] of [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)].entries()){
 if(match[1].trim())new vm.Script(match[1],{filename:'index-inline-'+i+'.js'});
}
new vm.Script(await readFile('webapp/market.js','utf8'),{filename:'market.js'});
console.log('All frontend JavaScript compiles');
