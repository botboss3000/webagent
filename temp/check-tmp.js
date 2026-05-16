const fs = require('fs');
const items = fs.readdirSync('C:\\Users\\Alex R\\AppData\\Local\\Temp').filter(i => i.startsWith('pi-subagents'));
console.log('Remaining pi-subagent dirs:', items);
