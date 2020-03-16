import pandas
import mymodule

# 4. Dataframe을 file으로 저장하기
friends = [
    {"name":"Kim", "age":20, "job":"Trash"},
    {"name":"Lee", "age":30, "job":"Animal"},
    {"name":"Park", "age":40, "job":"Streamer"},
    {"name":"Ryu", "age":50}
]
dataframe = pandas.DataFrame.from_dict(friends)
mymodule.printline(dataframe)

# csv 파일으로 출력하기 - to_csv(file_name)
dataframe.to_csv("friend.csv")
rf = open("friend.csv", 'r')
lines = rf.readlines()
for line in lines:
    print(line)
print('-' * 80)
rf.close()

# index = True, header = True 는 default 값이다.
# row number 0, 1, 2 를 넣고 싶지 않다면 index = False 를,
# column name 도 생략하고 싶다면 header = False 를 입력하면 된다.
dataframe.to_csv("friend.csv", index = False, header = False)
rf = open("friend.csv", 'r')
lines = rf.readlines()
for line in lines:
    print(line)
print('-' * 80)
rf.close()


# 빈 칸이 있을 때, 이것을 어떤 문자로 채울지를 na_rep 로 설정할 수 있다.
# (default는 None 혹은 NaN)
dataframe.to_csv("friend.csv", index = False, header = False, na_rep = '-')
rf = open("friend.csv", 'r')
lines = rf.readlines()
for line in lines:
    print(line)
print('-' * 80)
rf.close()
