UI
	- Fix mobile mode so it's full chat with ability to change to different windows
	 Should be able to enter own API key/provider
		- works but should be enhanced with "save key/model" feature so that there would be a list of preferences for quick switching

Connections
	- Need to test telegram
	- Need to add local whatsapp solution (portal to web-based whatsapp account on my number)

Message logic
	- Need to understand if messages using other sources would use websockets or POST/GET

Logic
	- Users from different sources can have same account, shared memory, etc
		○ Need to have registration and sync method
	- Session memory needs to be better
	- Ability to change sessions or resume sessions from a single chat. Can use /resume or /session /new command, or just be conversational. AI would then have ability to dynamically change session based on user's request
		○ Maybe even allow AI to change the session based on message context
	- Source based requirement, if from telegram, whatsapp, SMS, and other single window services, then session should change after a timeout UNLESS the context of the message is related to past conversation
		○ Advanced: AI could know if message context is related to 

Security
	- Need to have secrets db per userid
	- Need to enable encryption that would be linked per use
	- Need to have separate input and output encryption
	- Encryption keys to be reset periodically

Memory
	- Needs refinement/optimization
	- Should have ability to update is own md docs in the db with RLS

Skills
	- Needs refinement of revision logic

Advanced
	- Additional LLM loops to help (memory, skills, etc)
	- Ability to switch agents
	- Have ability for agent to start other agents, communicate with existing agents
	- Add "hyperlinks" that act like POST 

Prompt engineering
	- Smart brevity for consise output. Easier to read in message platforms

