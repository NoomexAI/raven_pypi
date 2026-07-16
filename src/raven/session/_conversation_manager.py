import sqlite3
import sqlite_vec
import json
import os
import logging
from datetime import datetime
import numpy as np
import uuid
import shutil
import gc

from raven.core.constants import CONVERSATION_DIR


logger = logging.getLogger(__name__)



class Conversation:
    def __init__(self, conv_path: str, base_model, embedding_model):
        self.conv_path = conv_path
        self.base_model = base_model
        self.embedding_model = embedding_model

        self.messages_db_path = os.path.join(conv_path, "messages.db")
        self.preferences_db_path = os.path.join(conv_path, "preferences.db")
        self.metadata_json_path = os.path.join(conv_path, "metadata.json")


        # Initialize messages.db
        with sqlite3.connect(self.messages_db_path) as conn:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)

            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    message_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                    role        TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                );
            """)
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_messages USING vec0(
                    message_id  INTEGER PRIMARY KEY,
                    embedding   FLOAT[1024]
                );
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS message_sections (
                    message_id INTEGER PRIMARY KEY,
                    sections_json TEXT NOT NULL,
                    FOREIGN KEY (message_id) REFERENCES messages (message_id) ON DELETE CASCADE
                );
            """)
            conn.commit()


        # Initialize preferences.db
        with sqlite3.connect(self.preferences_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS preferences (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    preferences_json TEXT NOT NULL
                );
            """)
            conn.commit()

        # Initialize empty metadata.json
        if not os.path.exists(self.metadata_json_path):
            with open(self.metadata_json_path, "w", encoding="utf-8") as f:
                pass

        logger.info(f"Conversation initialized at: {conv_path}")
        
    
    def set_metadata(self, conversation_id: str, knowledge_name: str = None, title: str = "New Conversation"):          #type: ignore
        try:
            self.conversation_id = conversation_id
            self.knowledge_name = knowledge_name
            self.type = "local" if self.knowledge_name is not None else "global"
            self.title = title
            self.pinned = False
            self.created_at = datetime.now().isoformat()

            metadata = {
                "conversation_id": self.conversation_id,
                "type": self.type,
                "knowledge_name": self.knowledge_name,
                "title": self.title,
                "pinned": self.pinned,
                "created_at": self.created_at
            }

            with open(self.metadata_json_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=4)

            logger.info(f"Metadata set for Conversation: {conversation_id}")

        except Exception as e:
            logger.error(f"Failed to set metadata for Conversation: {conversation_id}: {e}")
            raise


    def append_message(self, role: str, content: str):
        try:
            with sqlite3.connect(self.messages_db_path) as conn:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)

                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO messages (role, content, created_at)
                    VALUES (?, ?, ?)
                """, (role, content, datetime.now().isoformat()))

                message_id = cursor.lastrowid

                raw_vector = self.embedding_model.embed(content)
                binary_vector = np.array(raw_vector, dtype=np.float32).tobytes()

                cursor.execute("""
                    INSERT INTO vec_messages (message_id, embedding)
                    VALUES (?, ?)
                """, (message_id, binary_vector))

                conn.commit()
            
            return message_id

        except Exception as e:
            logger.error(f"Failed to append message to Conversation: {self.conversation_id}: {e}")
            raise

    
    def load_messages(self):
        try:
            with sqlite3.connect(self.messages_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT role, content, message_id
                    FROM messages 
                    ORDER BY message_id
                """)
                messages = [
                    {"role": row[0], "content": row[1], "message_id": row[2]}
                    for row in cursor.fetchall()
                ]

            logger.info(f"Loaded {len(messages)} messages for conversation: {self.conversation_id}")
            return messages

        except Exception as e:
            logger.error(f"Failed to load messages for conversation: {self.conversation_id}: {e}")
            raise

    
    def update_system_prompt(self, system_prompt: str):
        try:
            with sqlite3.connect(self.messages_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE messages 
                    SET content = ?
                    WHERE message_id = (
                        SELECT MIN(message_id) FROM messages WHERE role = 'system'
                    )
                """, (system_prompt,))
                conn.commit()
            logger.info(f"System prompt updated in messages.db for conversation: {self.conversation_id}")
        except Exception as e:
            logger.error(f"Failed to update system prompt: {e}")
            raise

    
    def load_metadata(self):
        try:
            with open(self.metadata_json_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)

            self.conversation_id = metadata["conversation_id"]
            self.type = metadata["type"]
            self.knowledge_name = metadata.get("knowledge_name")
            self.title = metadata["title"]
            self.pinned = metadata["pinned"]
            self.created_at = metadata["created_at"]

            logger.info(f"Loaded metadata for conversation: {self.conversation_id}")

        except Exception as e:
            logger.error(f"Failed to load metadata: {e}")
            raise


    def get_preference(self):
        try:
            with sqlite3.connect(self.preferences_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT preferences_json FROM preferences ORDER BY id DESC LIMIT 1")
                row = cursor.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error(f"Failed to load preferences for conversation: {self.conversation_id}: {e}")
            raise


    def update_title(self, title: str):
        try:
            with open(self.metadata_json_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)

            metadata["title"] = title
            self.title = title

            with open(self.metadata_json_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=4)

            logger.info(f"Updated title for conversation: {self.conversation_id} → {title}")

        except Exception as e:
            logger.error(f"Failed to update title for conversation: {self.conversation_id}: {e}")
            raise


    def toggle_pin(self):
        try:
            with open(self.metadata_json_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)

            metadata["pinned"] = not metadata["pinned"]
            self.pinned = metadata["pinned"]

            with open(self.metadata_json_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=4)

            logger.info(f"Pin toggled for conversation: {self.conversation_id} → {self.pinned}")

        except Exception as e:
            logger.error(f"Failed to toggle pin for conversation: {self.conversation_id}: {e}")
            raise

    
    def save_preference(self, preference: str):
        try:
            existing = self.get_preference()
            
            if existing:
                preferences = json.loads(existing)
            else:
                preferences = {"user_specifications": []}
            
            preferences["user_specifications"].append(preference)
            
            with sqlite3.connect(self.preferences_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT INTO preferences (preferences_json) VALUES (?)", 
                            (json.dumps(preferences),))
                conn.commit()
                
            logger.info(f"Preference saved for conversation: {self.conversation_id}")
        except Exception as e:
            logger.error(f"Failed to save preference: {e}")
            raise


    def save_retrieved_sections(self, message_id: int, sections: list):
        try:
            with sqlite3.connect(self.messages_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO message_sections (message_id, sections_json)
                    VALUES (?, ?)
                """, (message_id, json.dumps(sections)))
                conn.commit()
            logger.info(f"Saved sections for message: {message_id}")
        except Exception as e:
            logger.error(f"Failed to save message sections: {e}")
            raise

    def load_retrieved_sections(self, message_id: int) -> list:
        try:
            with sqlite3.connect(self.messages_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT sections_json FROM message_sections
                    WHERE message_id = ?
                """, (message_id,))
                row = cursor.fetchone()
                return json.loads(row[0]) if row else None                                          # type: ignore
        except Exception as e:
            logger.error(f"Failed to load message sections: {e}")
            raise





class ConversationManager:
    def __init__(self, knowledge_base, base_model, embedding_model, conversation_dir: str= CONVERSATION_DIR):
        self.knowledge_base = knowledge_base
        self.base_model = base_model
        self.embedding_model = embedding_model
        self.conversation_dir = conversation_dir
        self.registry = {}

        if not os.path.exists(self.conversation_dir):
            os.makedirs(self.conversation_dir)
            logger.info(f"Created conversations directory: {self.conversation_dir}")

        self.load_existing()


    def load_existing(self):
        try:
            for entry in os.listdir(self.conversation_dir):
                if entry.endswith(".conv"):
                    conversation_id = entry.replace(".conv", "")
                    conv_path = os.path.join(self.conversation_dir, entry)

                    conversation = Conversation(conv_path, self.base_model, self.embedding_model)
                    conversation.load_metadata()

                    self.registry[conversation_id] = conversation

            logger.info(f"Loaded {len(self.registry)} conversations")
        except Exception as e:
            logger.error(f"Failed to load existing conversations: {e}")
            raise


    def _generate_conversation_id(self):
        while True:
            conversation_id = uuid.uuid4().hex[:12]
            if conversation_id not in self.registry.keys():
                return conversation_id


    def create_conversation(self, knowledge_name: str = None):   #type: ignore
        try:
            if knowledge_name and knowledge_name not in self.knowledge_base.registry.keys():
                raise ValueError(f"Knowledge: {knowledge_name} does not exist")
            
            conversation_id = self._generate_conversation_id()
            conv_path = os.path.join(self.conversation_dir, f"{conversation_id}.conv")
            os.makedirs(conv_path)

            conversation = Conversation(conv_path, self.base_model, self.embedding_model)
            conversation.set_metadata(
                conversation_id=conversation_id,
                knowledge_name=knowledge_name
            )

            self.registry[conversation_id] = conversation

            logger.info(f"Created {conversation.type} conversation: {conversation_id}")
            return conversation_id

        except Exception as e:
            logger.error(f"Failed to create conversation: {e}")
            raise

    
    def get_conversation(self, conversation_id: str) -> Conversation:
        try:
            conversation = self.registry.get(conversation_id)
            if not conversation:
                logger.error(f"Conversation not found: {conversation_id}")
                raise KeyError(f"Conversation '{conversation_id}' not found")
            return conversation
        except Exception as e:
            logger.error(f"Failed to get conversation: {conversation_id}: {e}")
            raise


    def delete_conversation(self, conversation_id: str):
        try:
            conv_path = os.path.join(self.conversation_dir, f"{conversation_id}.conv")

            if not os.path.exists(conv_path):
                logger.error(f"Conversation not found: {conversation_id}")
                raise FileNotFoundError(f"Conversation '{conversation_id}' not found")
            
            del self.registry[conversation_id]
            gc.collect()
            shutil.rmtree(conv_path)
            
            logger.info(f"Deleted conversation: {conversation_id}")
        except Exception as e:
            logger.error(f"Failed to delete conversation: {conversation_id}: {e}")
            raise


    def list_conversations(self):
        try:
            conversations = [
                {
                    "conversation_id": conv.conversation_id,
                    "type": conv.type,
                    "knowledge_name": conv.knowledge_name,
                    "title": conv.title,
                    "pinned": conv.pinned,
                    "created_at": conv.created_at
                }
                for conv in self.registry.values()
            ]
            logger.info(f"Listed {len(conversations)} conversations")
            return conversations
        except Exception as e:
            logger.error(f"Failed to list conversations: {e}")
            raise


    def rename_conversation(self, conversation_id, title: str):
        conversation = self.get_conversation(conversation_id)
        if not title:
            return
        conversation.update_title(title)
