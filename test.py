import random
from quips import QUIPS
my_list = list(QUIPS)
random.shuffle(my_list)
for i in my_list:
    print('"'+i+'"')
