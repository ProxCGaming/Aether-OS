import asyncio
import logging
from typing import Any, Dict, List

logger = logging.getLogger("aether_engine.event_bus")

class EventBus:
    """
    A lightweight async pub-sub singleton.
    Allows nodes (like supervisor) to publish events (e.g., TOOL_ACTIVITY)
    that can be picked up by the executor without direct WebSocket access.
    """
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(EventBus, cls).__new__(cls)
            cls._instance._subscribers = {}
        return cls._instance
        
    def __init__(self):
        # Prevent re-initialization of instance variables if __init__ is called again
        if not hasattr(self, '_subscribers'):
            self._subscribers: Dict[str, List[asyncio.Queue]] = {}
            
    def subscribe(self, task_id: str, queue: asyncio.Queue):
        if task_id not in self._subscribers:
            self._subscribers[task_id] = []
        self._subscribers[task_id].append(queue)
        
    def unsubscribe(self, task_id: str, queue: asyncio.Queue = None):
        if task_id in self._subscribers:
            if queue:
                if queue in self._subscribers[task_id]:
                    self._subscribers[task_id].remove(queue)
                if not self._subscribers[task_id]:
                    del self._subscribers[task_id]
            else:
                del self._subscribers[task_id]
                
    def publish(self, task_id: str, event: Any):
        if task_id in self._subscribers:
            for queue in self._subscribers[task_id]:
                try:
                    queue.put_nowait(event)
                except Exception as e:
                    logger.error(f"Failed to publish event to queue for task {task_id}: {e}")

# Global singleton instance
event_bus = EventBus()
