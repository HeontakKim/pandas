import pandas as pd
import mymodule

# 중복된 row 삭제하기.

student_list = [
    ["Kim", "CS", "male"],  # 중복 1 
    ["Park", "CS", "male"],
    ["Choi", "EE", "male"],
    ["Ryu", "Physics", "male"],  # 중복 2
    ["Lee", "Economics", "female"],
    ["Kim", "CS", "male"],  # 중복 1
    ["Jenny", "CS", "female"],
    ["Kim", "CS", "male"],  # 중복 1
    ["John", "Physics", "male"],
    ["Ryu", "Physics", "male"]  # 중복 2
]
df = pd.DataFrame(student_list, columns=["name", "major", "sex"])
mymodule.printline(df)

# 1) duplicated method로, 어느 line이 중복인지를 확인 가능.
print("1) duplicated method로, 어느 line이 중복인지를 확인 가능.")
mymodule.printline(df.duplicated())

# 2) drop_duplicates method로, 중복된 row를 "모두" 제거 가능.
print("2) drop_duplicates method로, 중복된 row를 모두 제거 가능.")
mymodule.printline(df.drop_duplicates())

# 하나의 column만 같은 경우에만 제거하려면? (이름이 같은 경우)
student_list = [
    ["Kim", "CS", "male"],  # 중복 1 
    ["Park", "CS", "male"],
    ["Choi", "EE", "male"],
    ["Ryu", "Physics", "male"],  # 중복 2
    ["Lee", "Economics", "female"],
    ["Kim", "Physics", "female"],  # 중복 1
    ["Jenny", "CS", "female"],
    ["Kim", "EE", "male"],  # 중복 1
    ["John", "Physics", "male"],
    ["Ryu", "EE", "male"]  # 중복 2
]
df = pd.DataFrame(student_list, columns=["name", "major", "sex"])
print("New Dataframe:")
mymodule.printline(df)

# 3-1) 이 경우에는 정확하게 중복은 아니기 때문에, duplicated()에서 True가 나오지 않을 것
print("3-1) 이 경우에는 정확하게 중복은 아니기 때문에, duplicated()에서 True가 나오지 않을 것")
mymodule.printline(df.duplicated())

# 3-2) name만 일치해도 중복이라고 치고 싶으면, duplicated() 메소드의 인자로 ['name']을 넣어주어야 함.
print("3-2) name만 일치해도 중복이라고 치고 싶으면, duplicated() 메소드의 인자로 ['name']을 넣어주어야 함.")
mymodule.printline(df.duplicated(['name']))

# 4) drop_duplicates() 에서도 ['name']을 인자로 준다.
# 중복된 것 중 윗쪽을 남기고 싶으면 keep='first',
# 아래쪽을 남기고 싶으면 keep='last' 를 인자로 넣는다.
print("4-1) first")
mymodule.printline(df.drop_duplicates(['name'], keep='first'))
print("4-2) last")
mymodule.printline(df.drop_duplicates(['name'], keep='last'))