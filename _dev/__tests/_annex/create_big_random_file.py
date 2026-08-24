import random

def generate_diff_file(filename, num_lines=100000):  # 5mb
    with open(filename, 'w', encoding='utf-8') as f:
        for i in range(num_lines):
            # Introduce a mix of identical lines and randomized changes
            if i % 5 == 0:
                f.write(f"This is a static common line number {i}\n")
            elif i % 3 == 0:
                f.write(f"Modified content variant A at index {i} - {random.randint(1, 10000)}\n")
            else:
                f.write(f"Completely unique line {random.random()} for line {i}\n")

generate_diff_file("autogen_50mb_a.txt",1000000)
generate_diff_file("autogen_50mb_b.txt",1000000)

generate_diff_file("autogen_5mb_a.txt",100000)
generate_diff_file("autogen_5mb_b.txt",100000)

generate_diff_file("autogen_2mb_a.txt",40000)
generate_diff_file("autogen_2mb_b.txt",40000)
