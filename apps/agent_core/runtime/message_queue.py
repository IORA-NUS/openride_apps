# class RoleMessageQueueMixin:
#     """Shared in-memory queue helpers for app-level message buffering."""

#     def enqueue_message(self, payload):
#         self.message_queue.append(payload)

#     def dequeue_message(self):
#         try:
#             return self.message_queue.pop(0)
#         except Exception:
#             return None

#     def enfront_message(self, payload):
#         self.message_queue.insert(0, payload)
