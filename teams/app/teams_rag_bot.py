#!/usr/bin/env python3
"""
Microsoft Teams Bot Integration for Azure Search OpenAI RAG Demo
Integrates the validated chat client with Azure Bot Framework for Teams
Updated for latest Bot Framework SDK (no separate teams adapter needed)
"""
import pdb;
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional
import aiohttp
from datetime import datetime

# Bot Framework imports - Updated for latest SDK
from botbuilder.core import (
    ActivityHandler, 
    TurnContext, 
    MessageFactory,
    ConversationState,
    UserState,
    MemoryStorage,
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings
)
from botbuilder.core.conversation_state import ConversationState
from botbuilder.core.user_state import UserState
from botbuilder.core.teams import TeamsActivityHandler, TeamsInfo
from botbuilder.schema import (
    ChannelAccount, 
    Activity, 
    ActivityTypes,
    SuggestedActions,
    ActionTypes,
    CardAction,
    ResourceResponse
)
from aiohttp import web
from aiohttp.web import Request, Response, json_response

# Import your existing Pydantic models and client
from pydantic import BaseModel, Field, ValidationError
from enum import Enum
from dotenv import load_dotenv
load_dotenv()
import traceback


def log_exception(exc_type, exc_value, exc_traceback):
    logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
    logging.error("".join(traceback.format_exception(exc_type, exc_value, exc_traceback)))

sys.excepthook = log_exception


# Pydantic Models (from your existing code)
class MessageRole(str, Enum):
    """Enum for message roles"""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"

class Message(BaseModel):
    """Model for individual messages in the conversation"""
    content: str = Field(..., description="The content of the message")
    role: MessageRole = Field(..., description="The role of the message sender")

class RetrievalMode(str, Enum):
    """Enum for retrieval modes"""
    HYBRID = "hybrid"
    SEMANTIC = "semantic"
    KEYWORD = "keyword"

class GPT4VInput(str, Enum):
    """Enum for GPT4V input types"""
    TEXT_AND_IMAGES = "textAndImages"
    TEXT_ONLY = "textOnly"
    IMAGES_ONLY = "imagesOnly"

class Overrides(BaseModel):
    """Model for context overrides configuration"""
    top: int = Field(default=3, ge=1, description="Number of top results to return")
    temperature: float = Field(default=0.3, ge=0.0, le=2.0, description="Temperature for response generation")
    minimum_reranker_score: float = Field(default=0, ge=0.0, le=1.0, description="Minimum reranker score threshold")
    minimum_search_score: float = Field(default=0, ge=0.0, le=1.0, description="Minimum search score threshold")
    retrieval_mode: RetrievalMode = Field(default=RetrievalMode.HYBRID, description="Mode for document retrieval")
    semantic_ranker: bool = Field(default=True, description="Whether to use semantic ranking")
    semantic_captions: bool = Field(default=False, description="Whether to use semantic captions")
    query_rewriting: bool = Field(default=False, description="Whether to enable query rewriting")
    reasoning_effort: str = Field(default="", description="Level of reasoning effort")
    suggest_followup_questions: bool = Field(default=False, description="Whether to suggest follow-up questions")
    use_oid_security_filter: bool = Field(default=False, description="Whether to use OID security filter")
    use_groups_security_filter: bool = Field(default=False, description="Whether to use groups security filter")
    vector_fields: List[str] = Field(default=["embedding"], description="List of vector fields to use")
    use_gpt4v: bool = Field(default=False, description="Whether to use GPT-4V")
    gpt4v_input: GPT4VInput = Field(default=GPT4VInput.TEXT_AND_IMAGES, description="Type of input for GPT-4V")
    language: str = Field(default="en", description="Language code for responses")

class Context(BaseModel):
    """Model for request context"""
    overrides: Overrides = Field(default_factory=Overrides, description="Override settings for the request")

class ChatRequest(BaseModel):
    """Main model for the chat API request"""
    messages: List[Message] = Field(..., min_length=1, description="List of messages in the conversation")
    context: Optional[Context] = Field(default=None, description="Context settings for the request")
    session_state: Optional[Any] = Field(default=None, description="Session state data")

    class Config:
        """Pydantic configuration"""
        use_enum_values = True
        validate_assignment = True
        extra = "forbid"

# Enhanced Chat Client 
class ValidatedChatClient:
    def __init__(self, base_url: str, auth_token: Optional[str] = None):
        """Initialize the validated chat client"""
        self.base_url = base_url.rstrip('/')
        self.auth_token = auth_token
        self.session: Optional[aiohttp.ClientSession] = None
        
    async def __aenter__(self):
        """Async context manager entry"""
        headers = {'Content-Type': 'application/json'}
        if self.auth_token:
            headers['Authorization'] = f'Bearer {self.auth_token}'
            
        self.session = aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=300)
        )
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self.session:
            await self.session.close()
    
    def create_chat_request(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
        session_state: Optional[Dict[str, Any]] = None,
        overrides: Optional[Dict[str, Any]] = None
    ) -> ChatRequest:
        """Create and validate a ChatRequest object"""
        messages = []
        
        if history:
            for msg in history:
                messages.append(Message(
                    content=msg["content"],
                    role=MessageRole(msg["role"])
                ))
        
        messages.append(Message(
            content=message,
            role=MessageRole.USER
        ))
        
        context = None
        if overrides:
            context = Context(overrides=Overrides(**overrides))
        else:
            context = Context(overrides=Overrides())
        
        try:
            chat_request = ChatRequest(
                messages=messages,
                context=context,
                session_state=session_state
            )
            return chat_request
        except ValidationError as e:
            raise ValueError(f"Invalid chat request data: {e}")
    
    async def send_validated_chat_message(
        self, 
        message: str, 
        history: Optional[List[Dict[str, str]]] = None,
        session_state: Optional[Dict[str, Any]] = None,
        overrides: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Send a validated chat message to the /chat endpoint"""
        if not self.session:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")
        
        chat_request = self.create_chat_request(message, history, session_state, overrides)
        payload = chat_request.model_dump(exclude_none=False)
        url = f"{self.base_url}/chat"
        
        try:
            async with self.session.post(url, json=payload) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 401:
                    raise Exception("Authentication failed. Check your auth token.")
                elif response.status == 415:
                    raise Exception("Request must be JSON")
                else:
                    error_text = await response.text()
                    raise Exception(f"HTTP {response.status}: {error_text}")
                    
        except aiohttp.ClientError as e:
            raise Exception(f"Network error: {e}")

# Conversation Data Class
class ConversationData:
    def __init__(self):
        self.chat_history: List[Dict[str, str]] = []
        self.session_state: Optional[Dict[str, Any]] = None
        self.user_preferences: Dict[str, Any] = {
            "temperature": 0.3,
            "top": 3,
            "retrieval_mode": "hybrid",
            "semantic_ranker": True
        }

class UserProfile:
    def __init__(self):
        self.name: Optional[str] = None
        self.preferred_settings: Dict[str, Any] = {}

# Main Bot Class - Updated to use TeamsActivityHandler
class RAGTeamsBot(TeamsActivityHandler):
    """
    Main bot class that integrates with  RAG system
    Updated to use TeamsActivityHandler for Teams-specific functionality
    """
    
    def __init__(self, conversation_state: ConversationState, user_state: UserState, rag_client: ValidatedChatClient):
        super().__init__()
        self.conversation_state = conversation_state
        self.user_state = user_state
        self.rag_client = rag_client
        
        # State accessors
        self.conversation_data_accessor = self.conversation_state.create_property("ConversationData")
        self.user_profile_accessor = self.user_state.create_property("UserProfile")
        
        # Bot commands
        self.commands = {
            "/help": self._show_help,
            "/clear": self._clear_history,
            "/settings": self._show_settings,
            "/config": self._configure_settings,
            "/status": self._show_status
        }

    async def on_message_activity(self, turn_context: TurnContext):
        """Handle incoming messages"""
        try:
            # Get conversation data and user profile
            conversation_data = await self.conversation_data_accessor.get(
                turn_context, lambda: ConversationData()
            )
            user_profile = await self.user_profile_accessor.get(
                turn_context, lambda: UserProfile()
            )
            
            # Get user message
            user_message = turn_context.activity.text.strip()
            
            # Handle bot commands
            if user_message.startswith('/'):
                await self._handle_command(turn_context, user_message, conversation_data, user_profile)
                return
            
            # Show typing indicator
            #await self._send_typing_indicator(turn_context)
            
            # Process RAG query
            await self._process_rag_query(turn_context, user_message, conversation_data, user_profile)
            
        except Exception as e:
            logging.error(f"Error in on_message_activity: {e}")
            await turn_context.send_activity(
                MessageFactory.text(f" Sorry, I encountered an error: {str(e)}")
            )
        finally:
            # Save conversation state
            await self.conversation_state.save_changes(turn_context)
            await self.user_state.save_changes(turn_context)

    async def _process_rag_query(self, turn_context: TurnContext, user_message: str, 
                                conversation_data: ConversationData, user_profile: UserProfile):
        """Process the user query through RAG system"""
        try:
            # Prepare overrides from user preferences
            overrides = conversation_data.user_preferences.copy()
            
            # Send to RAG system
            response = await self.rag_client.send_validated_chat_message(
                message=user_message,
                history=conversation_data.chat_history,
                session_state=conversation_data.session_state,
                overrides=overrides
            )
            
            # Extract response content
            assistant_message = ""
            if 'message' in response and 'content' in response['message']:
                assistant_message = response['message']['content']
            elif 'choices' in response and len(response['choices']) > 0:
                assistant_message = response['choices'][0]['message']['content']
            else:
                assistant_message = "I received your message but couldn't generate a proper response."
            
            # Update conversation history
            conversation_data.chat_history.append({
                "role": "user", 
                "content": user_message
            })
            conversation_data.chat_history.append({
                "role": "assistant", 
                "content": assistant_message
            })
            
            # Update session state if available
            if 'session_state' in response:
                conversation_data.session_state = response['session_state']
            
            # Limit history to prevent it from growing too large
            if len(conversation_data.chat_history) > 20:
                conversation_data.chat_history = conversation_data.chat_history[-20:]
            
            # Send response to user
            await turn_context.send_activity(MessageFactory.text(assistant_message))
            
            # Add sources if available
            if 'context' in response and 'data_points' in response['context']:
                sources = response['context']['data_points']
                #if sources:
                    #sources_text = " **Sources:**\n" + "\n".join([f"• {source}" for source in sources])
                    #await turn_context.send_activity(MessageFactory.text(sources_text))
            
        except Exception as e:
            print(traceback.format_exc())
            logging.error(f"Error processing RAG query: {e}")
            await turn_context.send_activity(
                MessageFactory.text(f"Error processing your request: {str(e)}")
            )

    async def _handle_command(self, turn_context: TurnContext, command: str, 
                            conversation_data: ConversationData, user_profile: UserProfile):
        """Handle bot commands"""
        command_parts = command.split()
        base_command = command_parts[0].lower()
        
        if base_command in self.commands:
            await self.commands[base_command](turn_context, command_parts[1:], conversation_data, user_profile)
        else:
            await turn_context.send_activity(
                MessageFactory.text(f" Unknown command: {base_command}. Type /help for available commands.")
            )

    async def _show_help(self, turn_context: TurnContext, args: List[str], 
                        conversation_data: ConversationData, user_profile: UserProfile):
        """Show help message"""
        help_text = """
**RAG Chat Bot Commands:**

• `/help` - Show this help message
• `/clear` - Clear conversation history
• `/settings` - Show current settings
• `/config <setting> <value>` - Configure settings
• `/status` - Show bot status

**Available Settings:**
• `temperature` (0.0-2.0) - Response creativity
• `top` (1-10) - Number of search results
• `retrieval_mode` - Search mode (hybrid/semantic/keyword)
• `semantic_ranker` - Use semantic ranking (true/false)

**Example:** `/config temperature 0.7`

Just type your question to chat with the RAG system! 
        """
        await turn_context.send_activity(MessageFactory.text(help_text))

    async def _clear_history(self, turn_context: TurnContext, args: List[str], 
                           conversation_data: ConversationData, user_profile: UserProfile):
        """Clear conversation history"""
        conversation_data.chat_history = []
        conversation_data.session_state = None
        await turn_context.send_activity(
            MessageFactory.text(" Conversation history cleared!")
        )

    async def _show_settings(self, turn_context: TurnContext, args: List[str], 
                           conversation_data: ConversationData, user_profile: UserProfile):
        """Show current settings"""
        settings_text = "**Current Settings:**\n"
        for key, value in conversation_data.user_preferences.items():
            settings_text += f"• `{key}`: {value}\n"
        
        settings_text += f"\n **Conversation History:** {len(conversation_data.chat_history)} messages"
        await turn_context.send_activity(MessageFactory.text(settings_text))

    async def _configure_settings(self, turn_context: TurnContext, args: List[str], 
                                conversation_data: ConversationData, user_profile: UserProfile):
        """Configure bot settings"""
        if len(args) < 2:
            await turn_context.send_activity(
                MessageFactory.text(" Usage: `/config <setting> <value>`\nExample: `/config temperature 0.7`")
            )
            return
        
        setting = args[0].lower()
        value_str = args[1]
        
        try:
            # Validate and convert settings
            if setting == "temperature":
                value = float(value_str)
                if 0.0 <= value <= 2.0:
                    conversation_data.user_preferences[setting] = value
                else:
                    raise ValueError("Temperature must be between 0.0 and 2.0")
            elif setting == "top":
                value = int(value_str)
                if 1 <= value <= 10:
                    conversation_data.user_preferences[setting] = value
                else:
                    raise ValueError("Top must be between 1 and 10")
            elif setting == "retrieval_mode":
                if value_str.lower() in ["hybrid", "semantic", "keyword"]:
                    conversation_data.user_preferences[setting] = value_str.lower()
                else:
                    raise ValueError("Retrieval mode must be 'hybrid', 'semantic', or 'keyword'")
            elif setting == "semantic_ranker":
                if value_str.lower() in ["true", "false"]:
                    conversation_data.user_preferences[setting] = value_str.lower() == "true"
                else:
                    raise ValueError("Semantic ranker must be 'true' or 'false'")
            else:
                await turn_context.send_activity(
                    MessageFactory.text(f"Unknown setting: {setting}")
                )
                return
            
            await turn_context.send_activity(
                MessageFactory.text(f" Setting `{setting}` updated to `{conversation_data.user_preferences[setting]}`")
            )
            
        except ValueError as e:
            await turn_context.send_activity(
                MessageFactory.text(f" Invalid value for {setting}: {str(e)}")
            )

    async def _show_status(self, turn_context: TurnContext, args: List[str], 
                         conversation_data: ConversationData, user_profile: UserProfile):
        """Show bot status"""
        status_text = f"""
**RAG Bot Status:**

• **History:** {len(conversation_data.chat_history)} messages
• **Session:** {'Active' if conversation_data.session_state else 'New'}
• **RAG Endpoint:** {self.rag_client.base_url}
• **Authentication:** {' Configured' if self.rag_client.auth_token else 'Not configured'}

**Current Settings:**
"""
        for key, value in conversation_data.user_preferences.items():
            status_text += f"• {key}: {value}\n"
        
        await turn_context.send_activity(MessageFactory.text(status_text))

    async def _send_typing_indicator(self, turn_context: TurnContext):
        """Send typing indicator to show bot is processing"""
        typing_activity = MessageFactory.typing()
        await turn_context.send_activity(typing_activity)

    async def on_members_added_activity(self, members_added: List[ChannelAccount], turn_context: TurnContext):
        """Greet new members"""
        welcome_text = """
 **Welcome to the RAG Chat Bot!**

I'm here to help you search and get answers from your knowledge base using advanced AI.

• Type any question to get started
• Use `/help` to see available commands
• Use `/settings` to configure search preferences

Let's start chatting! 
        """
        
        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                await turn_context.send_activity(MessageFactory.text(welcome_text))


    async def on_teams_members_added(
        self,
        members_added: [ChannelAccount],
        team_info: TeamsInfo,
        turn_context: TurnContext
    ):
        for member in members_added:
            # Avoid sending welcome message to the bot itself
            if member.id != turn_context.activity.recipient.id:
                await turn_context.send_activity(f"Welcome to the team, {member.name}!")    # Your logic here


# Bot Adapter and Web Server Setup
def create_app():
    """Create the web application"""
    
    # Load configuration
    APP_ID = os.environ.get("MicrosoftAppId", "")
    APP_PASSWORD = os.environ.get("MicrosoftAppPassword", "")
            
    #RAG_BASE_URL = os.environ.get("RAG_BASE_URL", "http://localhost:9090")
    #RAG_AUTH_TOKEN = os.environ.get("RAG_AUTH_TOKEN", "")
    # RAG System Configuration
    RAG_BASE_URL = "http://localhost:50505"
    RAG_AUTH_TOKEN = ""
    
    # Create adapter settings
    settings = BotFrameworkAdapterSettings(
        app_id=APP_ID,
        app_password=APP_PASSWORD
    )
    
    # Create adapter - Using BotFrameworkAdapter instead of TeamsAdapter
    adapter = BotFrameworkAdapter(settings)
    
    # Create storage and state
    memory_storage = MemoryStorage()
    conversation_state = ConversationState(memory_storage)
    user_state = UserState(memory_storage)
    
    # Create RAG client
    rag_client = ValidatedChatClient(RAG_BASE_URL, RAG_AUTH_TOKEN)
    
    # Create bot
    bot = RAGTeamsBot(conversation_state, user_state, rag_client)
    
    # Error handler
    async def on_error(context: TurnContext, error: Exception):
        logging.error(f"Error: {error}")
        await context.send_activity(" Sorry, an error occurred. Please try again.")
    
    adapter.on_turn_error = on_error
    
    # Define the main messaging endpoint
    async def messages(req: Request) -> Response:
        body = await req.text()
        activity = Activity().deserialize(json.loads(body))
        auth_header = req.headers["Authorization"] if "Authorization" in req.headers else ""
        
        try:
            # Initialize RAG client for this request
            await rag_client.__aenter__()
            response = await adapter.process_activity(activity, auth_header, bot.on_turn)
            if response:
                return json_response(data=response.body, status=response.status)
            return Response(status=201)
        except Exception as e:
            logging.error(f"Error processing activity: {e}")
            return Response(status=500)
        finally:
            # Clean up RAG client
            await rag_client.__aexit__(None, None, None)
    
    # Health check endpoint
    async def health(req: Request) -> Response:
        return json_response({"status": "healthy", "timestamp": datetime.now().isoformat()})
    
    # Create web app
    app = web.Application()
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/health", health)
    app.router.add_get("/", lambda req: json_response({"message": "RAG Teams Bot is running!"}))
    
    return app

if __name__ == "__main__":
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Create and run the app
    app = create_app()
    
    try:
        port = int(os.environ.get("PORT", 3978))
        print(f"Starting RAG Teams Bot on port {port}")
        print(f"Health check: http://localhost:{port}/health")
        print(f"Bot endpoint: http://localhost:{port}/api/messages")
        
        web.run_app(app, host="0.0.0.0", port=port)
    except Exception as e:
        logging.error(f"Failed to start server: {e}")
        sys.exit(1)
