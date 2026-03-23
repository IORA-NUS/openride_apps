import re
import sys

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 script.py <file_path>")
        sys.exit(1)

    file_path = sys.argv[1]

    if not file_path.endswith(".txt"):
        print("File must end with .txt")
        sys.exit(1)

    with open(file_path, "r") as file:
        content = file.read()

    # Remove all non-alphabetic characters and convert to lowercase
    content = re.sub(r'[^a-zA-Z]', '', content).lower()

    # Count the frequency of each character
    char_count = {}
    for char in content:
        char_count[char] = char_count.get(char, 0) + 1

    # Sort the characters by frequency
    sorted_chars = sorted(char_count.items(), key=lambda x: x[1], reverse=True)

    # Print the result
    for char, count in sorted_chars:
        print(f"{char}: {count}")

if __name__ == "__main__":
    main()
