ARRAY TEXT($names; 0)
COMPONENT LIST($names)
var $result : Object
$result:={components: []; version: Application version(*)}
var $i : Integer
For ($i; 1; Size of array($names))
	$result.components.push($names{$i})
End for 
var $file : 4D.File
$file:=Folder(Folder(fk database folder; *).platformPath; fk platform path).file("result.json")
$file.setText(JSON Stringify($result; *))
QUIT 4D
