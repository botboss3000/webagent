UI
	- Fix mobile mode so it's full chat with ability to change to different windows
	 Should be able to enter own API key/provider
		- works but should be enhanced with "save key/model" feature so that there would be a list of preferences for quick switching

Connections
	- Need to test telegram
	- Need to add local whatsapp solution (portal to web-based whatsapp account on my number)

Message logic
	- Need to understand if messages using other sources would use websockets or POST/GET
	- users original message should inject beofre agent's final response to ensure it actually meet's the user's requirements. every re-injection does not need to be carried over. the agents state injection would only include the user's latest request.
	- optimize the agent prompt foe efficiency and error proofing and ease of read.

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


testing:
 - removing admin features
 - changing own context
 - 


 encryption:
 secret key for security database (has all secrets) - can be deleted/deactivated after production for true security, with EVEN the app owner not having decrypt ability. App is "publicly" open when hosted online. app is open sourced anyways, so the app data isn't special, but it's transparent showing there's no backdoors or "admin" controls.

 or does decryption require both admin and user keys available?

 each communication getting a unique secret key?
 
User typically does not need secret key since they are not typicalle using the database directly. They would primarily use messaging services, or the web portal. the web portal should have a seperate database for each user showing JUST the web portal communications. 

The User would have a seperate 2 factor authentication or password to link accounts in the web portal and do other things like read/edit their agent's context and interact with other select elements, but majority of communication would be "unreadable" to the web portal.

