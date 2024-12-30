import os

for i in range(0, 41):  # Loops from 8 to 50
    command = f"python clean.py neutrino_133119_22683750_galaxy{i} -x -o -g"
    os.system(command)