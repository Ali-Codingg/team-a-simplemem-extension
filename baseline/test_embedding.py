from simplemem.core.utils.embedding import EmbeddingModel
import time

start = time.time()

model = EmbeddingModel()
embedding = model.encode("Alice moved to Paris in 2024.")

print("Embedding dimension:", len(embedding))
print("Time:", round(time.time() - start, 2), "seconds")