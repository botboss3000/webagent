UI
	 Should be able to enter own API key/provider
		- works but should be enhanced with "save key/model" feature so that there would be a list of preferences for quick switching

bootstrap message to seed agent and user context

Connections
	- Need to test telegram
	- Need to add local whatsapp solution (portal to web-based whatsapp account on my number)

Message logic
	- Need to understand if messages using other sources would use websockets or POST/GET
	- users original message should inject beofre agent's final response to ensure it actually meet's the user's requirements. every re-injection does not need to be carried over. the agents state injection would only include the user's latest request.
	- optimize the agent prompt for efficiency and error proofing and ease of read.

User session logic
	- Users from different sources can have same account, shared memory, etc
		○ Need to have registration and sync method
	- cross-session memory needs to be better. did this get fixed with memory upgrades?
	- Ability to change sessions or resume sessions from a single chat. Can use /resume or /session /new command, or just be conversational. AI would then have ability to dynamically change session based on user's request
		○ Maybe even allow AI to change the session based on message context
		* Does db need to have 'active session' in user table?
	- Source based requirement, if from telegram, whatsapp, SMS, and other single window services, then session should change after a timeout UNLESS the context of the message is related to past conversation
		○ Advanced: AI could know if message context is related to 

Security
	- Need to have secrets db per userid
	- Need to enable encryption that would be linked per use
	- Need to have separate input and output encryption
	- Encryption keys to be reset periodically
	- 2 and maybe even 3 factor authentication
	different providers for secret key, db and llm keys?
	
Memory
	- Needs refinement/optimization/calibration
	- Should have ability to update is own md docs in the db with RLS

Skills
	- Needs refinement of revision logic
	- rating/performance
	improvement based on user input
	issue tracking by devs
	- created tools would be per userid only, but would get reviewed by devs to include in produciton release

Issues and dev contact
	- need to have method to send messages to devs for issues, errors, feature requests, etc


Advanced
	- Additional LLM loops to help (memory, skills, etc)
	- Ability to switch agents
	- Have ability for agent to start other agents, communicate with existing agents
	- Add "hyperlinks" that act like POST 

Prompt engineering
	- Smart brevity for consise output. Easier to read in message platforms
	- response MUST be in same language as user's message

Saveable agents
- agents could call other agents (different context window, different memorydb/preferences/knowledge/different LLM)

testing:
 - removing admin features
 - changing own context

 UI Style: check out "Hermes Agent Creative Visualization Skills" video by Onchain AI Garage

 autoagent if modifying its own code would need to check that it can run first in a test 
