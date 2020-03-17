import pandas
import mymodule

# 5강. 각 Row와 Column을 어떻게 select하고, filter하는가
friends = [
    {"name":"Kim", "age":20, "job":"Trash"},
    {"name":"Lee", "age":30, "job":"Animal"},
    {"name":"Park", "age":40, "job":"Streamer"},
    {"name":"Ryu", "age":50}
]
dataframe = pandas.DataFrame.from_dict(friends)
print("DataFrame:")
mymodule.printline(dataframe)

# 1) row_index 로 row select하기
sub_df = dataframe[1:3]
print(" 1) row_index 로 row select하기")
mymodule.printline(sub_df)

# 2) row_index 로 불연속적인 row select하기
sub_df = dataframe.loc[[0, 3]]
print(" 2) row_index 로 불연속적인 row select하기")
mymodule.printline(sub_df)

# 3) column 조건에 따라 row select하기
sub_df = dataframe[dataframe.age > 35]
print(" 3) column 조건에 따라 row select하기")
mymodule.printline(sub_df)

# 3-2) query 를 이용해 똑같은 짓 하기
sub_df = dataframe.query('age < 35')
print(" 3-2) query 를 이용해 똑같은 짓 하기")
mymodule.printline(sub_df)

# 3-3) 여러가지 조건
sub_df = dataframe[(dataframe.age < 45) & (dataframe.job != "Trash")]
print(" 3-3) 여러가지 조건")
mymodule.printline(sub_df)

# 새로운 dataframe
friend_list = [
    ["Kim", 20, "Python"],
    ["Lee", 30, "C++"],
    ["Park", 40, "Java"],
    ["Ryu", 50, "Visual basic"]
]
dataframe = pandas.DataFrame.from_records(friend_list)
print(" 다른 DataFrame")
mymodule.printline(dataframe)

# 4) column index에 따라 column select하기
# [row, column]
sub_df = dataframe.iloc[:,:2]
print(" 4) column index에 따라 column select하기")
mymodule.printline(sub_df)

# 5) column index에 따라 column select하기(불연속적 select)
sub_df = dataframe.iloc[:, [0,2]]
print(" 5) column index에 따라 column select하기(불연속적 select)")
mymodule.printline(sub_df)

# 6) column name으로 column select하기
dataframe.columns = ["name", "age", "language"]

sub_df = dataframe[["language", "age"]]
print(" 6) column name으로 column select하기")
mymodule.printline(sub_df)

# 6-2) filter 함수로 똑같은 짓 하기
sub_df = dataframe.filter(items=["language", "name"])
print(" 6-2) filtered 함수로 똑같은 짓 하기")
mymodule.printline(sub_df)

# 6-3) filter 함수의 like 이용해 column name에 'age'가 들어간 것만 select하기
sub_df = dataframe.filter(like="age", axis=1)
print(" 6-3) filter 함수의 like 이용해 column name에 'age'가 들어간 것만 select하기")
mymodule.printline(sub_df)

# 6-4) filter 함수의 regex 이용해 정규식 이용하여 column name select하기
sub_df = dataframe.filter(regex=".*age", axis=1)
print(" 6-4) filter 함수의 regex 이용해 정규식 이용하여 column name select하기")
mymodule.printline(sub_df)